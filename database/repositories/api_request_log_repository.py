from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from database.models import ApiRequestLog


class ApiRequestLogRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_log(
        self,
        method: str,
        path: str,
        status_code: int,
        duration_ms: float,
        user_email: Optional[str] = None,
        client_ip: Optional[str] = None,
        query_params: Optional[str] = None,
        request_body: Optional[str] = None,
        response_body: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> ApiRequestLog:
        """Insert one audit log row for a completed API request."""
        log = ApiRequestLog(
            method=method,
            path=path,
            status_code=status_code,
            duration_ms=duration_ms,
            user_email=user_email,
            client_ip=client_ip,
            query_params=query_params,
            request_body=request_body,
            response_body=response_body,
            error_message=error_message,
        )
        self.session.add(log)
        await self.session.flush()
        return log
