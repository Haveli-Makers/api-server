"""
Async client for InfluxDB 3 Core over its HTTP API: SQL queries, line-protocol writes, and creating the database
with its retention period. Used by the bot health watcher and the /metrics endpoints.
"""
import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)


def parse_time(value: str) -> datetime:
    """InfluxDB returns UTC times without a zone and with up to 9 decimals, e.g. 2026-11-24T17:04:46.566565123."""
    text = value.rstrip("Z")
    if "." in text:
        whole, fraction = text.split(".", 1)
        text = f"{whole}.{fraction[:6]}"
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


class InfluxDBError(Exception):
    """InfluxDB answered with an error."""


class InfluxDBUnavailable(InfluxDBError):
    """InfluxDB could not be reached."""


class QueryTooLarge(InfluxDBError):
    """The query would read more files than InfluxDB 3 Core allows: roughly 72 hours of one table."""


class InfluxDBClient:
    def __init__(self, url: str, database: str, token: str = "", timeout: float = 30.0):
        self._url = url.rstrip("/")
        self.database = database
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: Optional[aiohttp.ClientSession] = None

    async def query(self, sql: str, missing_table_ok: bool = False) -> List[Dict[str, Any]]:
        """
        Run a SQL query and return its rows. With missing_table_ok, a table (or database) nobody has written to
        yet gives an empty list instead of an error.
        """
        status, body = await self._request(
            "GET", "/api/v3/query_sql", params={"db": self.database, "q": sql, "format": "json"})
        if status == 200:
            return json.loads(body) if body.strip() else []
        _raise_if_refused(status)
        message = _first_line(body) or f"HTTP {status}"
        if missing_table_ok and "not found" in body.lower():
            return []
        if "file limit" in body.lower():
            raise QueryTooLarge(message)
        raise InfluxDBError(f"InfluxDB query failed: {message}")

    async def write(self, lines: Iterable[Optional[str]]):
        """Write line-protocol points (None entries are skipped)."""
        payload = "\n".join(line for line in lines if line)
        if not payload:
            return
        status, body = await self._request(
            "POST", "/api/v3/write_lp", params={"db": self.database, "precision": "nanosecond"},
            data=payload.encode())
        if status >= 300:
            _raise_if_refused(status)
            raise InfluxDBError(f"InfluxDB write failed: {_first_line(body) or f'HTTP {status}'}")

    async def ensure_database(self, retention: str):
        """Create the database with its retention period. A database that already exists is left as it is."""
        status, body = await self._request(
            "POST", "/api/v3/configure/database", json_body={"db": self.database, "retention_period": retention})
        if status < 300:
            logger.info(f"Created InfluxDB database '{self.database}' (data kept {retention})")
        elif status == 409:
            logger.info(f"InfluxDB database '{self.database}' exists")
        else:
            _raise_if_refused(status)
            raise InfluxDBError(f"Creating InfluxDB database '{self.database}' failed: "
                                f"{_first_line(body) or f'HTTP {status}'}")

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, method: str, path: str, params: Optional[Dict[str, str]] = None,
                       data: Optional[bytes] = None, json_body: Optional[Dict[str, Any]] = None) -> Tuple[int, str]:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout, headers=self._headers)
        try:
            async with self._session.request(method, f"{self._url}{path}", params=params, data=data,
                                             json=json_body) as response:
                return response.status, await response.text()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
            raise InfluxDBUnavailable(f"InfluxDB at {self._url} is unreachable: {e!r}") from e


def _first_line(body: str) -> str:
    return body.strip().splitlines()[0][:400] if body.strip() else ""


def _raise_if_refused(status: int):
    if status in (401, 403):
        raise InfluxDBError(f"InfluxDB refused the token (HTTP {status}). Check INFLUXDB_TOKEN.")
