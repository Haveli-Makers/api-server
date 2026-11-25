"""
Bot health from the heartbeats bots write to InfluxDB. Every few seconds the watcher reads each bot's latest
heartbeats, decides up or down per level, writes one bot_up point per bot and level, and records every change in
bot_status_change (and the log). It sends no notifications yet.

Each bot writes two heartbeats to bot_heartbeat (tag level):
  process   every 5 s from a background thread; stops only when the process dies
  strategy  every 1 s from the strategy loop; stops when the loop freezes or the process dies
So a silent strategy loop while the process still beats is a hang, and both silent is a crash.

A bot that stops on purpose writes a "stopped" event to bot_lifecycle. The watcher then marks it stopped instead of
down, and writes no bot_up points for it until it beats again. Bots silent for 6 hours are no longer watched.
"""
import asyncio
import logging
import time
from typing import Dict, List, Optional, Tuple

from services.influxdb_client import InfluxDBClient, parse_time
from utils.line_protocol import to_line

logger = logging.getLogger(__name__)

WATCH_HOURS = 6
LATEST_HEARTBEATS = ("SELECT bot, level, max(time) AS last FROM bot_heartbeat "
                     f"WHERE time > now() - INTERVAL '{WATCH_HOURS} hours' GROUP BY bot, level")
LIFECYCLE_EVENTS = ("SELECT bot, event, time FROM bot_lifecycle "
                    f"WHERE time > now() - INTERVAL '{WATCH_HOURS} hours' ORDER BY time DESC")
# The process heartbeat comes every 5 s. "Hung?" needs one within the last two beats: during a crash the process
# is still inside its timeout for a while, but its heartbeat is already stale.
PROCESS_FRESH_S = 10.0
# The process heartbeat can land a few seconds after the "stopped" event of a clean stop.
STOP_GRACE_S = 10.0


class BotHealthWatcher:
    def __init__(self, client: InfluxDBClient, retention: str = "14d", interval: float = 5.0,
                 process_timeout: float = 20.0, strategy_timeout: float = 10.0):
        self._client = client
        self._retention = retention
        self.interval = interval
        self._timeouts = {"process": process_timeout, "strategy": strategy_timeout}
        self._states: Dict[Tuple[str, str], Tuple[bool, float]] = {}  # (bot, level) -> (up, since)
        self._stopped: Dict[str, float] = {}  # bot -> when it stopped
        self._first_check = True
        self._database_ready = False
        self._task: Optional[asyncio.Task] = None
        self.last_check: Optional[float] = None
        self.last_error: Optional[str] = None
        self.bots_watched = 0

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run())
            logger.info("Bot health watcher started")

    async def stop(self):
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def check(self):
        """One round: read the heartbeats and lifecycle events, write bot_up and any status changes."""
        if not self._database_ready:
            await self._client.ensure_database(self._retention)
            self._database_ready = True
        heartbeats = await self._client.query(LATEST_HEARTBEATS, missing_table_ok=True)
        lifecycle = await self._client.query(LIFECYCLE_EVENTS, missing_table_ok=True)
        await self._client.write(self.evaluate(heartbeats, lifecycle, time.time()))
        self.last_check = time.time()

    def evaluate(self, heartbeats: List[Dict], lifecycle: List[Dict], now: float) -> List[str]:
        """
        heartbeats: {"bot", "level", "last"} rows; lifecycle: {"bot", "event", "time"} rows, newest first.
        Returns the points to write. The first check after a start records states without reporting changes,
        so restarting the API raises no false alarms.
        """
        last_seen: Dict[str, Dict[str, float]] = {}
        for row in heartbeats:
            if row.get("bot") and row.get("level") in self._timeouts and row.get("last"):
                last_seen.setdefault(row["bot"], {})[row["level"]] = parse_time(row["last"]).timestamp()
        latest_event: Dict[str, Tuple[str, float]] = {}
        for row in lifecycle:
            if row.get("bot") and row.get("time") and row["bot"] not in latest_event:
                latest_event[row["bot"]] = (row.get("event"), parse_time(row["time"]).timestamp())

        report = not self._first_check
        timestamp_ns = int(now * 1e9)
        lines: List[str] = []
        for bot, levels in sorted(last_seen.items()):
            event = latest_event.get(bot)
            if event and event[0] == "stopped" and max(levels.values()) <= event[1] + STOP_GRACE_S:
                if bot not in self._stopped:
                    self._stopped[bot] = event[1]
                    for level in self._timeouts:
                        self._states.pop((bot, level), None)
                    if report:
                        lines.append(self._status_change(bot, "bot", "stopped", f"{bot}: stopped", timestamp_ns))
                continue
            if self._stopped.pop(bot, None) is not None and report:
                lines.append(self._status_change(bot, "bot", "started", f"{bot}: started", timestamp_ns))

            process_beating = "process" in levels and now - levels["process"] <= PROCESS_FRESH_S
            for level, seen in sorted(levels.items()):
                up = now - seen <= self._timeouts[level]
                lines.append(to_line("bot_up", {"bot": bot, "level": level}, {"up": int(up)}, timestamp_ns))
                message = self._on_change(bot, level, up, now, process_beating)
                if message and report:
                    lines.append(self._status_change(bot, level, "up" if up else "down", message, timestamp_ns))

        for key in [key for key in self._states if key[0] not in last_seen]:
            del self._states[key]
        for bot in [bot for bot in self._stopped if bot not in last_seen]:
            del self._stopped[bot]
        self.bots_watched = len(last_seen)
        self._first_check = False
        return lines

    def _on_change(self, bot: str, level: str, up: bool, now: float, process_beating: bool) -> Optional[str]:
        previous = self._states.get((bot, level))
        if previous is not None and previous[0] == up:
            return None
        self._states[(bot, level)] = (up, now)
        if previous is None:
            return None
        what = "strategy loop" if level == "strategy" else "process"
        if up:
            return f"{bot}: {what} back after {_duration(now - previous[1])}"
        if level == "strategy":
            text = f"{bot}: strategy loop silent for {self._timeouts['strategy']:.0f} s"
            return text + (" while the process is alive (hung?)" if process_beating else "")
        return f"{bot}: process silent for {self._timeouts['process']:.0f} s, bot down"

    @staticmethod
    def _status_change(bot: str, level: str, state: str, message: str, timestamp_ns: int) -> str:
        (logger.warning if state == "down" else logger.info)(f"Bot status: {message}")
        return to_line("bot_status_change", {"bot": bot, "level": level, "state": state}, {"message": message},
                       timestamp_ns)

    async def _run(self):
        while True:
            try:
                await self.check()
                if self.last_error:
                    logger.info("Bot health watcher: InfluxDB reachable again")
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception as e:  # keep watching; log each new problem once, not every round
                message = str(e) or repr(e)
                if message != self.last_error:
                    logger.warning(f"Bot health watcher: {message}")
                self.last_error = message
            await asyncio.sleep(self.interval)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(round(seconds)), 60)
    if not minutes:
        return f"{secs} s"
    return f"{minutes} min {secs} s" if secs else f"{minutes} min"
