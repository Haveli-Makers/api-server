import asyncio
import json
import logging
import time
from typing import Any, Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from database.repositories import ApiRequestLogRepository

logger = logging.getLogger(__name__)

MAX_BODY_CHARS = 8000

EXCLUDED_PATHS = {"/", "/docs", "/openapi.json", "/redoc", "/favicon.ico"}

REDACT_KEYS = {
    "password", "secret", "token", "api_key", "apikey", "private_key",
    "authorization", "cookie_secret", "client_secret",
}


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("***" if k.lower() in REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    return value


def _truncate_body(raw: bytes) -> Optional[str]:
    if not raw:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"<binary, {len(raw)} bytes>"

    try:
        parsed = json.loads(text)
        text = json.dumps(_redact(parsed))
    except (json.JSONDecodeError, ValueError):
        pass  

    if len(text) > MAX_BODY_CHARS:
        text = text[:MAX_BODY_CHARS] + f"...<truncated, {len(text)} total chars>"
    return text


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in EXCLUDED_PATHS:
            return await call_next(request)

        start = time.monotonic()

        request_body = await request.body()  # Starlette caches this; safe to read here.

        error_message = None
        try:
            response = await call_next(request)
        except Exception as exc:
            error_message = str(exc)
            duration_ms = (time.monotonic() - start) * 1000
            self._log_in_background(
                request=request,
                status_code=500,
                duration_ms=duration_ms,
                request_body=request_body,
                response_body=b"",
                error_message=error_message,
            )
            raise

        response_body = b"".join([chunk async for chunk in response.body_iterator])
        response = Response(
            content=response_body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

        duration_ms = (time.monotonic() - start) * 1000
        self._log_in_background(
            request=request,
            status_code=response.status_code,
            duration_ms=duration_ms,
            request_body=request_body,
            response_body=response_body,
            error_message=None,
        )

        return response

    def _log_in_background(
        self,
        request: Request,
        status_code: int,
        duration_ms: float,
        request_body: bytes,
        response_body: bytes,
        error_message: Optional[str],
    ) -> None:
        db_manager = getattr(request.app.state, "db_manager", None)
        if db_manager is None:
            return  # e.g. a request that raced app startup

        user_email = request.headers.get("x-user-email")
        client_ip = request.client.host if request.client else None
        query_params = str(request.query_params) if request.query_params else None

        asyncio.create_task(
            self._write_log(
                db_manager=db_manager,
                method=request.method,
                path=request.url.path,
                status_code=status_code,
                duration_ms=duration_ms,
                user_email=user_email,
                client_ip=client_ip,
                query_params=query_params,
                request_body=_truncate_body(request_body),
                response_body=_truncate_body(response_body),
                error_message=error_message,
            )
        )

    @staticmethod
    async def _write_log(db_manager, **kwargs) -> None:
        try:
            async with db_manager.get_session_context() as session:
                await ApiRequestLogRepository(session).create_log(**kwargs)
        except Exception:
            logger.exception("Failed to write API request audit log")
