"""
Bot monitoring: reads back what bots write to InfluxDB (heartbeats, prices, orders, PnL), what account monitors write
(balances, open orders, fills and order book samples of one exchange account), what the market recorder writes (order
book samples), and what the bot health watcher writes (bot_up, bot_status_change). Every endpoint except
/metrics/health needs INFLUXDB_ENABLED=true; see "Bot monitoring" in the README for the tables.

Windows are given in minutes, from 5 up to 4320 (72 hours: InfluxDB 3 Core reads at most about 72 hours of a table
in one query), and end at `end` (UTC) or now.
"""
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from config import settings
from deps import get_influxdb_client
from services.influxdb_client import InfluxDBClient, InfluxDBError, InfluxDBUnavailable, QueryTooLarge, parse_time
from utils.order_segments import FILL_EVENTS, build_segments

router = APIRouter(tags=["Bot Monitoring"], prefix="/metrics")

MAX_MINUTES = 72 * 60
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
# bot_up points further apart than this mean the watcher was not running in between.
GAP_S = 30


def _minutes(default: int):
    return Query(default=default, ge=5, le=MAX_MINUTES, description="Window length in minutes (up to 72 hours)")


END = Query(default=None, description="End of the window (UTC); now if left out")
EXCHANGE = Query(default=None, description="Only this exchange (for a bot that trades several markets)")
PAIR = Query(default=None, description="Only this trading pair (for a bot that trades several markets)")


# ---------------------------------------------------------------------------------------------------- health


@router.get("/health")
async def get_health(request: Request):
    """Whether bot monitoring is on, whether InfluxDB answers, and how the bot health watcher is doing."""
    client: Optional[InfluxDBClient] = getattr(request.app.state, "influxdb_client", None)
    watcher = getattr(request.app.state, "bot_health_watcher", None)
    if client is None:
        return {"enabled": False}
    reachable, error = True, None
    try:
        await client.query("SELECT 1")
    except InfluxDBError as e:
        reachable, error = False, str(e)
    return {
        "enabled": True,
        "database": client.database,
        "influxdb_reachable": reachable,
        "influxdb_error": error,
        "watcher": None if watcher is None else {
            "running": watcher.running,
            "last_check": _iso_epoch(watcher.last_check) if watcher.last_check else None,
            "last_error": watcher.last_error,
            "bots_watched": watcher.bots_watched,
        },
    }


# ---------------------------------------------------------------------------------------------- bot health


@router.get("/uptime")
async def get_uptime(minutes: int = _minutes(360), end: Optional[datetime] = END,
                     client: InfluxDBClient = Depends(get_influxdb_client)):
    """Uptime % per bot and level (process, strategy) over the window. Time a bot was stopped on purpose is left
    out."""
    return await _query(client, "SELECT bot, level, avg(up) * 100 AS uptime_pct, count(*) AS samples FROM bot_up "
                                f"WHERE {_window(minutes, end)[0]} GROUP BY bot, level ORDER BY bot, level")


@router.get("/uptime/timeline")
async def get_uptime_timeline(minutes: int = _minutes(360), end: Optional[datetime] = END,
                              client: InfluxDBClient = Depends(get_influxdb_client)):
    """Up and down runs per bot and level, ready to draw as a timeline."""
    return [{**segment, "start": _iso(segment["start"]), "end": _iso(segment["end"])}
            for segment in await _uptime_segments(client, minutes, end)]


@router.get("/incidents")
async def get_incidents(minutes: int = _minutes(1440), end: Optional[datetime] = END,
                        client: InfluxDBClient = Depends(get_influxdb_client)):
    """
    Down periods per bot, newest first. kind is "hang" when the strategy loop stopped while the process kept
    beating, "crash" when the process stopped too, and "no heartbeat" when only the process heartbeat stopped.
    """
    down = [segment for segment in await _uptime_segments(client, minutes, end) if segment["state"] == "down"]
    strategy = [segment for segment in down if segment["level"] == "strategy"]
    process = [segment for segment in down if segment["level"] == "process"]

    def overlapping(segment: Dict, others: List[Dict]) -> List[Dict]:
        return [other for other in others if other["bot"] == segment["bot"]
                and other["start"] < segment["end"] and segment["start"] < other["end"]]

    incidents = []
    for segment in strategy:
        crashed = overlapping(segment, process)
        parts = [segment] + crashed
        incidents.append({"bot": segment["bot"], "kind": "crash" if crashed else "hang",
                          "start": min(part["start"] for part in parts), "end": max(part["end"] for part in parts),
                          "ongoing": any(part["ongoing"] for part in parts)})
    for segment in process:
        if not overlapping(segment, strategy):
            incidents.append({"bot": segment["bot"], "kind": "no heartbeat", "start": segment["start"],
                              "end": segment["end"], "ongoing": segment["ongoing"]})
    incidents.sort(key=lambda incident: incident["start"], reverse=True)
    return [{**incident, "duration_s": round((incident["end"] - incident["start"]).total_seconds()),
             "start": _iso(incident["start"]), "end": _iso(incident["end"])} for incident in incidents]


@router.get("/status-changes")
async def get_status_changes(minutes: int = _minutes(1440), end: Optional[datetime] = END,
                             bot: Optional[str] = Query(default=None),
                             limit: int = Query(default=50, ge=1, le=500),
                             client: InfluxDBClient = Depends(get_influxdb_client)):
    """What the bot health watcher noticed, newest first: down, up again, stopped, started."""
    bot_filter = f" AND bot = '{_name(bot, 'bot')}'" if bot else ""
    return await _query(client, "SELECT time, bot, level, state, message FROM bot_status_change "
                                f"WHERE {_window(minutes, end)[0]}{bot_filter} ORDER BY time DESC LIMIT {limit}")


# --------------------------------------------------------------------------------------------- bots and PnL


@router.get("/bots")
async def get_bots(client: InfluxDBClient = Depends(get_influxdb_client)):
    """Bots that reported a price in the last day, with the exchange and pair they trade."""
    return await _query(client, "SELECT DISTINCT bot, exchange, pair FROM bot_price "
                                "WHERE time > now() - INTERVAL '1 day' ORDER BY bot, exchange, pair")


@router.get("/pnl")
async def get_pnl(minutes: int = _minutes(360), end: Optional[datetime] = END, bot: Optional[str] = Query(default=None),
                  client: InfluxDBClient = Depends(get_influxdb_client)):
    """PnL per bot and controller, averaged per time bucket (10 seconds to 15 minutes, by window length)."""
    bot_filter = f" AND bot = '{_name(bot, 'bot')}'" if bot else ""
    return await _query(
        client,
        f"SELECT date_bin(INTERVAL '{_pnl_bucket(minutes)}', time) AS time, bot, controller, "
        "avg(global_pnl_quote) AS global_pnl_quote, avg(realized_pnl_quote) AS realized_pnl_quote, "
        "avg(unrealized_pnl_quote) AS unrealized_pnl_quote, max(volume_traded) AS volume_traded "
        f"FROM controller_performance WHERE {_window(minutes, end)[0]}{bot_filter} GROUP BY 1, 2, 3 ORDER BY 1")


@router.get("/orders")
async def get_orders(bot: str, minutes: int = _minutes(60), end: Optional[datetime] = END,
                     exchange: Optional[str] = EXCHANGE, pair: Optional[str] = PAIR,
                     client: InfluxDBClient = Depends(get_influxdb_client)):
    """
    Everything the orders-vs-price chart needs for one bot: its price, one segment per order (placed until filled,
    cancelled or now), its fills (partial and final), and the grid's reference price. exchange and pair narrow it
    to one market, for a bot that trades several.
    """
    bot = _name(bot, "bot")
    window, start, stop = _window(minutes, end)
    bot_window = f"bot = '{bot}' AND {window}"
    where = bot_window + _market_filter(exchange, pair)
    bucket = _price_bucket_seconds(minutes)
    if bucket is None:
        price = await _query(client, f"SELECT time, best_bid, best_ask, mid_price FROM bot_price WHERE {where} "
                                     "ORDER BY time")
    else:
        price = await _query(client, f"SELECT date_bin(INTERVAL '{bucket} seconds', time) AS time, "
                                     "avg(best_bid) AS best_bid, avg(best_ask) AS best_ask, avg(mid_price) AS mid_price "
                                     f"FROM bot_price WHERE {where} GROUP BY 1 ORDER BY 1")
    events = await _query(client, f"SELECT * FROM order_event WHERE {where} ORDER BY time")
    snapshots = await _query(client, "SELECT order_id, side, min(time) AS first_seen, max(time) AS last_seen, "
                                     "avg(price) AS price, max(amount) AS amount, max(level) AS level "
                                     f"FROM open_order WHERE {where} GROUP BY order_id, side")
    # One snapshot lists the open orders of every market the bot trades, so the latest one is looked up across all
    # of them: a market with nothing open writes no rows, and its last order must still count as gone.
    latest = await _query(client, f"SELECT max(time) AS t FROM open_order WHERE {bot_window}")
    reference = await _query(client, f"SELECT time, reference_price FROM grid_state WHERE {where} ORDER BY time")

    segments = build_segments(
        events=[{**event, "time": _epoch(event["time"])} for event in events],
        snapshots=[{**snapshot, "first_seen": _epoch(snapshot["first_seen"]),
                    "last_seen": _epoch(snapshot["last_seen"])} for snapshot in snapshots],
        window_start=start, window_end=stop,
        latest_snapshot=_epoch(latest[0]["t"]) if latest and latest[0].get("t") else None)
    for segment in segments:
        segment["start"] = _iso_epoch(segment["start"])
        segment["end"] = _iso_epoch(segment["end"])
    fills = [{"time": event["time"], "order_id": event["order_id"], "side": event["side"], "price": event["price"],
              "amount": event["amount"], "partial": event["event"] == "partially_filled", "level": event.get("level"),
              "realized_pnl": event.get("realized_pnl")}
             for event in events if event.get("event") in FILL_EVENTS]
    return {"bot": bot, "exchange": exchange, "pair": pair, "price": price, "segments": segments, "fills": fills,
            "reference": reference}


@router.get("/ladder")
async def get_ladder(bot: str, exchange: Optional[str] = EXCHANGE, pair: Optional[str] = PAIR,
                     client: InfluxDBClient = Depends(get_influxdb_client)):
    """
    One bot right now: the orders in its latest open-order snapshot, its price, grid state and PnL. exchange and
    pair narrow it to one market, for a bot that trades several.
    """
    bot = _name(bot, "bot")
    market = _market_filter(exchange, pair)
    rows = await _query(client, f"SELECT * FROM open_order WHERE bot = '{bot}' "
                                "AND time > now() - INTERVAL '1 minute' ORDER BY time DESC")
    orders, snapshot_age = [], None
    if rows:
        newest = _epoch(rows[0]["time"])  # the latest snapshot, which covers every market the bot trades
        mine = [row for row in rows if (not exchange or row.get("exchange") == exchange)
                and (not pair or row.get("pair") == pair)]
        if mine:
            snapshot_age = time.time() - _epoch(mine[0]["time"])
        if time.time() - newest <= 15:  # a bot that stopped writes no snapshots, so an old one means "nothing open"
            orders = [row for row in mine if newest - _epoch(row["time"]) < 1]

    async def latest(table: str, condition: str = "") -> Optional[Dict]:
        found = await _query(client, f"SELECT * FROM {table} WHERE bot = '{bot}'{condition} "
                                     "AND time > now() - INTERVAL '5 minutes' ORDER BY time DESC LIMIT 1")
        return found[0] if found else None

    state = await latest("grid_state", market)
    # PnL rows carry no market, only the controller: with a market picked, use the controller whose grid trades it.
    controller = (state or {}).get("controller")
    by_controller = f" AND controller = '{controller}'" if market and controller and SAFE_NAME.match(controller) else ""
    return {"bot": bot, "exchange": exchange, "pair": pair, "orders": sorted(orders, key=lambda order: -order["price"]),
            "snapshot_age_s": snapshot_age, "price": await latest("bot_price", market), "state": state,
            "performance": await latest("controller_performance", by_controller)}


# ------------------------------------------------------------------------------------------------- accounts

ACCOUNT = Query(description="The account's name, as its account monitor writes it")
ACCOUNT_EXCHANGE = Query(description="The exchange of that account")
# InfluxDB answers times like 2026-11-26T07:00:00.123456789; only that shape goes back into SQL.
STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,9})?Z?$")


@router.get("/accounts")
async def get_accounts(client: InfluxDBClient = Depends(get_influxdb_client)):
    """
    Accounts an account monitor wrote about in the last day, each with its latest update: when (age_s), what it
    could read (balances_ok, orders_ok, fills_ok), counts, and any error.
    """
    rows = await _query(
        client,
        "SELECT * FROM (SELECT *, row_number() OVER (PARTITION BY account, exchange ORDER BY time DESC) AS newest "
        "FROM account_update WHERE time > now() - INTERVAL '1 day') WHERE newest = 1 ORDER BY account, exchange")
    now = time.time()
    return [{**{key: value for key, value in row.items() if key != "newest"}, "age_s": now - _epoch(row["time"])}
            for row in rows]


@router.get("/balances")
async def get_balances(account: str = ACCOUNT, exchange: str = ACCOUNT_EXCHANGE, minutes: int = _minutes(360),
                       end: Optional[datetime] = END, client: InfluxDBClient = Depends(get_influxdb_client)):
    """
    One account's balances: `latest`, what it holds now per asset (assets at zero left out), and `history`, total and
    available per asset over the window: the last value in each time bucket, since a balance is a level (an average
    would hide a balance that dropped to zero mid-bucket).
    """
    where = _account_filter(account, exchange)
    latest = await _query(
        client,
        "SELECT asset, total, available, time FROM (SELECT asset, total, available, time, "
        "row_number() OVER (PARTITION BY asset ORDER BY time DESC) AS newest "
        f"FROM account_balance WHERE {where} AND time > now() - INTERVAL '1 day') "
        "WHERE newest = 1 AND total > 0 ORDER BY asset")
    history = await _query(
        client,
        f"SELECT date_bin(INTERVAL '{_pnl_bucket(minutes)}', time) AS time, asset, "
        "last_value(total ORDER BY time) AS total, last_value(available ORDER BY time) AS available "
        f"FROM account_balance WHERE {where} AND {_window(minutes, end)[0]} GROUP BY 1, 2 ORDER BY 1, 2")
    return {"account": account, "exchange": exchange, "latest": latest, "history": history}


@router.get("/account-orders")
async def get_account_orders(account: str = ACCOUNT, exchange: str = ACCOUNT_EXCHANGE,
                             client: InfluxDBClient = Depends(get_influxdb_client)):
    """
    The account's open orders now, whoever placed them (any bot, or a person): the rows of the latest update that
    read them, highest price first, and snapshot_age_s, how old that read is. `error` is the latest update's error.
    """
    where = _account_filter(account, exchange)
    latest = await _query(client, f"SELECT time, error FROM account_update WHERE {where} "
                                  "AND time > now() - INTERVAL '1 day' ORDER BY time DESC LIMIT 1")
    read = await _query(client, f"SELECT time, open_orders FROM account_update WHERE {where} AND orders_ok = true "
                                "AND time > now() - INTERVAL '1 day' ORDER BY time DESC LIMIT 1")
    answer = {"account": account, "exchange": exchange, "orders": [], "snapshot_age_s": None,
              "error": (latest[0].get("error") or None) if latest else None}
    if not read:
        return answer
    stamp = str(read[0]["time"])
    answer["snapshot_age_s"] = time.time() - _epoch(stamp)
    if not read[0].get("open_orders") or not STAMP.match(stamp):
        return answer
    # The update's rows share its time, plus one nanosecond per row.
    at = f"to_timestamp('{stamp.rstrip('Z')}Z')"
    orders = await _query(client, f"SELECT * FROM account_open_order WHERE {where} AND time >= {at} "
                                  f"AND time < {at} + INTERVAL '1 millisecond'")
    answer["orders"] = sorted(orders, key=lambda order: -(order.get("price") or 0))
    return answer


@router.get("/fills")
async def get_fills(account: str = ACCOUNT, exchange: str = ACCOUNT_EXCHANGE, minutes: int = _minutes(1440),
                    end: Optional[datetime] = END, pair: Optional[str] = Query(default=None, description="Only this pair"),
                    limit: int = Query(default=500, ge=1, le=5000),
                    client: InfluxDBClient = Depends(get_influxdb_client)):
    """The account's fills in the window, newest first: price, amount, quote amount and fee of each trade."""
    where = _account_filter(account, exchange) + (f" AND pair = '{_name(pair, 'pair')}'" if pair else "")
    return await _query(client, f"SELECT * FROM account_fill WHERE {where} AND {_window(minutes, end)[0]} "
                                f"ORDER BY time DESC LIMIT {limit}")


# ------------------------------------------------------------------------------------------------- markets


@router.get("/markets")
async def get_markets(client: InfluxDBClient = Depends(get_influxdb_client)):
    """Markets the market recorder sampled in the last day."""
    return await _query(client, "SELECT DISTINCT exchange, pair FROM market WHERE time > now() - INTERVAL '1 day' "
                                "ORDER BY exchange, pair")


@router.get("/feeds")
async def get_feeds(client: InfluxDBClient = Depends(get_influxdb_client)):
    """Per market: the latest sample, and book_age_s, the seconds since its order book last changed."""
    rows = await _query(client, "SELECT exchange, pair, time, book_age_s, mid_price, spread_pct FROM market "
                                "WHERE time > now() - INTERVAL '2 minutes' ORDER BY time DESC")
    latest: Dict[Tuple[str, str], Dict] = {}
    for row in rows:
        latest.setdefault((row["exchange"], row["pair"]), row)
    return [{**row, "sample_age_s": time.time() - _epoch(row["time"])} for row in latest.values()]


@router.get("/market")
async def get_market(exchange: str, pair: str, minutes: int = _minutes(60), end: Optional[datetime] = END,
                     client: InfluxDBClient = Depends(get_influxdb_client)):
    """One market over time: best bid and ask, mid price, spread %, quote depth within 1% on each side, book age."""
    exchange, pair = _name(exchange, "exchange"), _name(pair, "pair")
    bucket = _price_bucket_seconds(minutes) or 1
    return await _query(
        client,
        f"SELECT date_bin(INTERVAL '{bucket} seconds', time) AS time, avg(best_bid) AS best_bid, "
        "avg(best_ask) AS best_ask, avg(mid_price) AS mid_price, "
        "avg(spread_pct) AS spread_pct, avg(bid_depth_quote_1_0) AS bid_depth_1pct, "
        "avg(ask_depth_quote_1_0) AS ask_depth_1pct, max(book_age_s) AS book_age_s "
        f"FROM market WHERE exchange = '{exchange}' AND pair = '{pair}' AND {_window(minutes, end)[0]} "
        "GROUP BY 1 ORDER BY 1")


# ------------------------------------------------------------------------------------------------- helpers


async def _query(client: InfluxDBClient, sql: str) -> List[Dict]:
    """Run a query; a table nobody has written to yet gives no rows. InfluxDB problems become HTTP errors."""
    try:
        return await client.query(sql, missing_table_ok=True)
    except QueryTooLarge as e:
        raise HTTPException(status_code=422, detail=f"Window too large for InfluxDB; use a shorter one. {e}")
    except InfluxDBUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))
    except InfluxDBError as e:
        raise HTTPException(status_code=502, detail=str(e))


async def _uptime_segments(client: InfluxDBClient, minutes: int, end: Optional[datetime]) -> List[Dict]:
    """
    Up and down runs per bot and level. InfluxDB finds the changes itself (a window function), so a 3-day window
    costs a few dozen rows instead of every point the watcher wrote.
    """
    window = _window(minutes, end)[0]
    step = timedelta(seconds=settings.influxdb.watcher_interval)
    changes = await _query(
        client,
        "SELECT bot, level, time, up, prev_time FROM ("
        " SELECT bot, level, time, up,"
        "  lag(up) OVER (PARTITION BY bot, level ORDER BY time) AS prev_up,"
        "  lag(time) OVER (PARTITION BY bot, level ORDER BY time) AS prev_time"
        f" FROM bot_up WHERE {window})"
        f" WHERE prev_up IS NULL OR up <> prev_up OR time - prev_time > INTERVAL '{GAP_S} seconds'"
        " ORDER BY bot, level, time")
    last_seen = {(row["bot"], row["level"]): parse_time(row["last"]) for row in await _query(
        client, f"SELECT bot, level, max(time) AS last FROM bot_up WHERE {window} GROUP BY bot, level")}

    segments: List[Dict] = []
    open_segments: Dict[Tuple[str, str], Dict] = {}
    for row in changes:
        key = (row["bot"], row["level"])
        at = parse_time(row["time"])
        current = open_segments.get(key)
        if current is not None:
            previous = parse_time(row["prev_time"]) if row.get("prev_time") else None
            after_gap = previous is not None and (at - previous).total_seconds() > GAP_S
            current["end"] = previous + step if after_gap else at
            segments.append(current)
        open_segments[key] = {"bot": key[0], "level": key[1], "state": "up" if row["up"] else "down", "start": at}
    for key, current in open_segments.items():
        current["end"] = last_seen.get(key, current["start"]) + step
        current["ongoing"] = True
        segments.append(current)
    for segment in segments:
        segment.setdefault("ongoing", False)
        segment["duration_s"] = round((segment["end"] - segment["start"]).total_seconds())
    return segments


def _account_filter(account: str, exchange: str) -> str:
    return f"account = '{_name(account, 'account')}' AND exchange = '{_name(exchange, 'exchange')}'"


def _name(value: str, what: str) -> str:
    """Names go into SQL, so only letters, digits and . _ - are allowed."""
    if not SAFE_NAME.match(value):
        raise HTTPException(status_code=400, detail=f"Invalid {what}: {value!r}")
    return value


def _market_filter(exchange: Optional[str], pair: Optional[str]) -> str:
    """The SQL for the optional exchange and pair filters; empty when neither is given."""
    return ((f" AND exchange = '{_name(exchange, 'exchange')}'" if exchange else "")
            + (f" AND pair = '{_name(pair, 'pair')}'" if pair else ""))


def _window(minutes: int, end: Optional[datetime]) -> Tuple[str, float, float]:
    """The SQL time filter, and the window's start and end as epoch seconds."""
    if end is None:
        stop = time.time()
        return f"time > now() - INTERVAL '{minutes} minutes'", stop - minutes * 60, stop
    end = _utc(end)
    stamp = end.strftime("%Y-%m-%dT%H:%M:%SZ")
    return (f"time > to_timestamp('{stamp}') - INTERVAL '{minutes} minutes' AND time <= to_timestamp('{stamp}')",
            end.timestamp() - minutes * 60, end.timestamp())


def _pnl_bucket(minutes: int) -> str:
    return ("10 seconds" if minutes <= 60 else "1 minute" if minutes <= 360
            else "5 minutes" if minutes <= 1440 else "15 minutes")


def _price_bucket_seconds(minutes: int) -> Optional[int]:
    """None means every point (short windows)."""
    return None if minutes <= 30 else 5 if minutes <= 120 else 30 if minutes <= 720 else 120


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_epoch(value: float) -> str:
    return _iso(datetime.fromtimestamp(value, tz=timezone.utc))


def _epoch(value: str) -> float:
    return parse_time(value).timestamp()
