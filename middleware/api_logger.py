import base64
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from database.models import ApiLog

logger = logging.getLogger(__name__)

_MAX_BODY_BYTES = 64 * 1024  
_SKIP_PATHS = {"/", "/docs", "/openapi.json", "/redoc"}


def _decode_body(raw: bytes) -> str | None:
    if not raw:
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        text = base64.b64encode(raw).decode()
    if len(raw) > _MAX_BODY_BYTES:
        text = text[: _MAX_BODY_BYTES] + "…[truncated]"
    return text


class APILoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP_PATHS:
            return await call_next(request)

        start = time.perf_counter()

        raw_request = await request.body()

        username: str | None = None
        try:
            import base64 as _b64
            auth = request.headers.get("authorization", "")
            if auth.lower().startswith("basic "):
                decoded = _b64.b64decode(auth[6:]).decode("utf-8", errors="replace")
                username = decoded.split(":", 1)[0] or None
        except Exception:
            pass

        response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000

        raw_response = b"".join([chunk async for chunk in response.body_iterator])

        await self._save_log(
            request=request,
            raw_request=raw_request,
            raw_response=raw_response,
            status_code=response.status_code,
            duration_ms=duration_ms,
            username=username,
        )

        return Response(
            content=raw_response,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    async def _save_log(
        self,
        request: Request,
        raw_request: bytes,
        raw_response: bytes,
        status_code: int,
        duration_ms: float,
        username: str | None,
    ) -> None:
        try:
            db_manager = request.app.state.db_manager
        except AttributeError:
            return 

        query_string = request.url.query or None

        record = ApiLog(
            method=request.method,
            path=request.url.path,
            query_string=query_string,
            request_body=_decode_body(raw_request),
            response_body=_decode_body(raw_response),
            status_code=status_code,
            duration_ms=round(duration_ms, 3),
            username=username,
        )

        try:
            async with db_manager.get_session_context() as session:
                session.add(record)
        except Exception as exc:
            logger.warning("Failed to persist API log: %s", exc)
