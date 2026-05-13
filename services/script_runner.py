import asyncio
import json
import os
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from models.scripts import ScriptRunRequest, ScriptRunResult, ScriptSchedule, ScriptScheduleCreate


SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validate_name(value: str, field_name: str) -> str:
    if not value or not SAFE_NAME.match(value) or ".." in value:
        raise ValueError(f"Invalid {field_name}: {value}")
    return value[:-3] if value.endswith(".py") else value


def _interval_delta(value: int, unit: str) -> timedelta:
    if unit == "minutes":
        return timedelta(minutes=value)
    if unit == "hours":
        return timedelta(hours=value)
    if unit == "weeks":
        return timedelta(weeks=value)
    raise ValueError(f"Unsupported interval unit: {unit}")


class HummingbotSDKScriptBackend:
    """Adapter boundary for the external hummingbot-sdk script runner."""

    async def run(self, request: ScriptRunRequest) -> Optional[ScriptRunResult]:
        try:
            from hummingbot_sdk.scripts import run_script  # type: ignore
        except ImportError:
            return None

        started_at = _utc_now()
        run_id = str(uuid.uuid4())
        try:
            maybe_result = run_script(
                strategy_name=request.strategy_name,
                config_name=request.config_name,
                account_name=request.account_name,
                verbose=request.verbose,
                extra_args=request.extra_args,
            )
            if asyncio.iscoroutine(maybe_result):
                maybe_result = await maybe_result
            output = maybe_result if isinstance(maybe_result, str) else json.dumps(maybe_result, default=str)
            status = "success"
            return_code = 0
        except Exception as exc:
            output = str(exc)
            status = "failed"
            return_code = 1

        return ScriptRunResult(
            run_id=run_id,
            strategy_name=request.strategy_name,
            config_name=request.config_name,
            account_name=request.account_name,
            started_at=started_at,
            completed_at=_utc_now(),
            status=status,
            output=output,
            return_code=return_code,
        )


class LocalProcessScriptBackend:
    """Runs locally available scripts until hummingbot-sdk provides the runner."""

    def __init__(self, bots_path: str = "bots"):
        self.bots_path = Path(bots_path)

    def _script_path(self, strategy_name: str) -> Path:
        candidates = [
            self.bots_path / "strategies" / f"{strategy_name}.py",
            self.bots_path / "scripts" / f"{strategy_name}.py",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return candidate
        raise FileNotFoundError(f"Strategy script '{strategy_name}' was not found")

    def _config_arg(self, config_name: str) -> str:
        candidates = [
            self.bots_path / "conf" / "scripts" / f"{config_name}.yml",
            self.bots_path / "conf" / "scripts" / f"{config_name}.json",
            self.bots_path / "conf" / f"{config_name}.yml",
            self.bots_path / "conf" / f"{config_name}.json",
            self.bots_path / f"{config_name}.yml",
            self.bots_path / f"{config_name}.json",
        ]
        for candidate in candidates:
            if candidate.exists() and candidate.is_file():
                return os.path.relpath(candidate, self.bots_path)
        return config_name

    async def run(self, request: ScriptRunRequest) -> ScriptRunResult:
        started_at = _utc_now()
        run_id = str(uuid.uuid4())
        script_path = self._script_path(request.strategy_name)
        cmd = ["python3", os.path.relpath(script_path, self.bots_path)]
        if request.account_name:
            cmd.extend(["--account", request.account_name])
        cmd.extend(["--config", self._config_arg(request.config_name)])
        if request.verbose:
            cmd.append("-vd")
        cmd.extend(request.extra_args)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=self.bots_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = "\n".join(
            part.decode("utf-8", errors="replace").strip()
            for part in [stdout, stderr]
            if part
        )
        return ScriptRunResult(
            run_id=run_id,
            strategy_name=request.strategy_name,
            config_name=request.config_name,
            account_name=request.account_name,
            started_at=started_at,
            completed_at=_utc_now(),
            status="success" if proc.returncode == 0 else "failed",
            output=output,
            return_code=proc.returncode,
        )


class ScriptRunnerService:
    def __init__(self, storage_path: str = "bots/script_scheduler"):
        self.storage_path = Path(storage_path)
        self.schedules_file = self.storage_path / "schedules.json"
        self.history_path = self.storage_path / "history"
        self.sdk_backend = HummingbotSDKScriptBackend()
        self.local_backend = LocalProcessScriptBackend()
        self._schedules: Dict[str, ScriptSchedule] = {}
        self._running_schedule_ids: set[str] = set()
        self._task: Optional[asyncio.Task] = None
        self._lock = asyncio.Lock()

    async def start(self):
        self.storage_path.mkdir(parents=True, exist_ok=True)
        self.history_path.mkdir(parents=True, exist_ok=True)
        await self._load_schedules()
        self._task = asyncio.create_task(self._scheduler_loop())

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._save_schedules()

    async def run_instant(self, request: ScriptRunRequest) -> ScriptRunResult:
        request.strategy_name = _validate_name(request.strategy_name, "strategy_name")
        request.config_name = _validate_name(request.config_name, "config_name")
        sdk_result = await self.sdk_backend.run(request)
        if sdk_result is not None:
            return sdk_result
        return await self.local_backend.run(request)

    async def create_schedule(self, request: ScriptScheduleCreate) -> ScriptSchedule:
        request.strategy_name = _validate_name(request.strategy_name, "strategy_name")
        request.config_name = _validate_name(request.config_name, "config_name")
        schedule = ScriptSchedule(
            id=str(uuid.uuid4()),
            created_at=_utc_now(),
            next_run_at=_utc_now() + _interval_delta(request.interval_value, request.interval_unit),
            **request.model_dump(),
        )
        async with self._lock:
            self._schedules[schedule.id] = schedule
            await self._save_schedules()
        return schedule

    async def list_schedules(self) -> List[ScriptSchedule]:
        async with self._lock:
            return sorted(self._schedules.values(), key=lambda item: item.created_at, reverse=True)

    async def delete_schedule(self, schedule_id: str) -> Dict[str, str]:
        async with self._lock:
            if schedule_id not in self._schedules:
                raise KeyError(schedule_id)
            del self._schedules[schedule_id]
            await self._save_schedules()
        return {"message": f"Schedule '{schedule_id}' deleted"}

    async def run_schedule_now(self, schedule_id: str) -> ScriptRunResult:
        async with self._lock:
            schedule = self._schedules.get(schedule_id)
            if schedule is None:
                raise KeyError(schedule_id)
        return await self._run_schedule(schedule)

    async def get_history(self, schedule_id: str, limit: int = 50) -> List[ScriptRunResult]:
        history_file = self.history_path / f"{schedule_id}.json"
        if not history_file.exists():
            return []
        data = json.loads(history_file.read_text(encoding="utf-8"))
        return [ScriptRunResult(**item) for item in data[-limit:]][::-1]

    async def _scheduler_loop(self):
        while True:
            now = _utc_now()
            schedules = await self.list_schedules()
            for schedule in schedules:
                if schedule.enabled and schedule.next_run_at <= now and schedule.id not in self._running_schedule_ids:
                    asyncio.create_task(self._run_schedule(schedule))
            await asyncio.sleep(30)

    async def _run_schedule(self, schedule: ScriptSchedule) -> ScriptRunResult:
        self._running_schedule_ids.add(schedule.id)
        try:
            request = ScriptRunRequest(
                strategy_name=schedule.strategy_name,
                config_name=schedule.config_name,
                account_name=schedule.account_name,
                verbose=schedule.verbose,
                extra_args=schedule.extra_args,
            )
            try:
                result = await self.run_instant(request)
            except Exception as exc:
                now = _utc_now()
                result = ScriptRunResult(
                    run_id=str(uuid.uuid4()),
                    strategy_name=schedule.strategy_name,
                    config_name=schedule.config_name,
                    account_name=schedule.account_name,
                    started_at=now,
                    completed_at=now,
                    status="failed",
                    output=str(exc),
                    return_code=1,
                )
            await self._append_history(schedule.id, result)
            async with self._lock:
                current = self._schedules.get(schedule.id)
                if current:
                    current.last_run_at = result.completed_at
                    current.next_run_at = result.completed_at + _interval_delta(current.interval_value, current.interval_unit)
                    await self._save_schedules()
            return result
        finally:
            self._running_schedule_ids.discard(schedule.id)

    async def _append_history(self, schedule_id: str, result: ScriptRunResult):
        history_file = self.history_path / f"{schedule_id}.json"
        data = []
        if history_file.exists():
            data = json.loads(history_file.read_text(encoding="utf-8"))
        data.append(json.loads(result.model_dump_json()))
        history_file.write_text(json.dumps(data[-50:], indent=2), encoding="utf-8")

    async def _load_schedules(self):
        if not self.schedules_file.exists():
            self._schedules = {}
            return
        data = json.loads(self.schedules_file.read_text(encoding="utf-8"))
        self._schedules = {item["id"]: ScriptSchedule(**item) for item in data}

    async def _save_schedules(self):
        self.storage_path.mkdir(parents=True, exist_ok=True)
        payload = [json.loads(schedule.model_dump_json()) for schedule in self._schedules.values()]
        self.schedules_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
