import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator, Dict, Optional

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from .models import Base

logger = logging.getLogger(__name__)

POOL_SIZE = 5
POOL_MAX_OVERFLOW = 10
POOL_MONITOR_INTERVAL_S = 60


class AsyncDatabaseManager:
    def __init__(self, database_url: str):
        # Convert postgresql:// to postgresql+asyncpg:// for async support
        if database_url.startswith("postgresql://"):
            database_url = database_url.replace("postgresql://", "postgresql+asyncpg://")
            
        self.engine = create_async_engine(
            database_url,
            # Connection pool settings for async
            pool_size=POOL_SIZE,
            max_overflow=POOL_MAX_OVERFLOW,
            pool_timeout=10,  # Fail fast instead of hanging requests when the pool is exhausted
            pool_recycle=1800,  # Recycle connections after 30 minutes
            pool_pre_ping=True,  # Test connections before using them
            # Engine settings
            echo=False,  # Set to True for SQL query logging
            echo_pool=False,  # Set to True for connection pool logging
            # Connection arguments for asyncpg
            connect_args={
                "server_settings": {
                    "application_name": "hummingbot-api",
                    "idle_in_transaction_session_timeout": "300000",
                    "statement_timeout": "60000",
                },
                "command_timeout": 60,
            }
        )
        self._register_timeout_invalidation()
        self.async_session = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
        self._initialized = False
        self._pool_monitor_task: Optional[asyncio.Task] = None

    def _register_timeout_invalidation(self):
        """
        Treat asyncpg timeouts as disconnects.

        When asyncpg's command_timeout fires mid-transaction (e.g. a BEGIN or pre-ping that
        never gets a reply over a slow network), SQLAlchemy would otherwise keep treating the
        connection as usable: the pool slot is never released and Postgres is left holding an
        "idle in transaction" session. Flagging the error as a disconnect makes the pool close
        and discard the connection instead.
        """
        @event.listens_for(self.engine.sync_engine, "handle_error")
        def _invalidate_on_timeout(context):
            if isinstance(context.original_exception, TimeoutError):
                logger.warning(f"Database call timed out, discarding connection: {context.original_exception!r}")
                context.is_disconnect = True
    
    async def ensure_initialized(self):
        """
        Ensure database is initialized before using it.
        This method is idempotent and can be called multiple times safely.
        """
        if not self._initialized:
            await self.create_tables()
            self._initialized = True
    
    @property
    def is_initialized(self) -> bool:
        """Check if the database has been initialized."""
        return self._initialized
        
    async def create_tables(self):
        """Create all tables defined in the models."""
        try:
            async with self.engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)

                # Drop Hummingbot's native tables since we use our custom orders/trades tables
                await self._drop_hummingbot_tables(conn)

            logger.info("Database tables created successfully")
        except Exception as e:
            logger.error(f"Failed to create database tables: {e}")
            raise

    async def _drop_hummingbot_tables(self, conn):
        """Drop Hummingbot's native database tables since we use custom ones."""
        hummingbot_tables = [
            "hummingbot_orders",
            "hummingbot_trade_fills", 
            "hummingbot_order_status"
        ]
        
        for table_name in hummingbot_tables:
            try:
                await conn.execute(text(f"DROP TABLE IF EXISTS {table_name}"))
                logger.info(f"Dropped Hummingbot table: {table_name}")
            except Exception as e:
                logger.debug(f"Could not drop table {table_name}: {e}")  # Use debug since table might not exist
            
    async def close(self):
        """Close all database connections."""
        await self.stop_pool_monitor()
        await self.engine.dispose()
        logger.info("Database connections closed")
        
    def get_session(self) -> AsyncSession:
        """Get a new database session."""
        return self.async_session()
    
    @asynccontextmanager
    async def get_session_context(self) -> AsyncGenerator[AsyncSession, None]:
        """
        Get a database session with automatic error handling and cleanup.
        
        Usage:
            async with db_manager.get_session_context() as session:
                # Use session here
        """
        session = self.async_session()
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await asyncio.shield(session.close())

    def pool_status(self) -> Dict[str, int]:
        """Current connection pool usage."""
        pool = self.engine.pool
        return {
            "checked_out": pool.checkedout(),
            "idle": pool.checkedin(),
            "max_connections": POOL_SIZE + POOL_MAX_OVERFLOW,
        }

    def start_pool_monitor(self):
        """Periodically log pool usage so a slow leak shows up before the pool is exhausted."""
        if self._pool_monitor_task is None:
            self._pool_monitor_task = asyncio.create_task(self._pool_monitor_loop())

    async def stop_pool_monitor(self):
        if self._pool_monitor_task:
            self._pool_monitor_task.cancel()
            try:
                await self._pool_monitor_task
            except asyncio.CancelledError:
                pass
            self._pool_monitor_task = None

    async def _pool_monitor_loop(self):
        while True:
            await asyncio.sleep(POOL_MONITOR_INTERVAL_S)
            status = self.pool_status()
            message = f"DB pool: {status['checked_out']}/{status['max_connections']} connections in use, {status['idle']} idle"
            if status["checked_out"] >= status["max_connections"] - 2:
                logger.warning(f"{message} - pool nearly exhausted")
            else:
                logger.info(message)

    async def health_check(self) -> bool:
        """
        Check if the database connection is healthy.
        
        Returns:
            bool: True if connection is healthy, False otherwise.
        """
        try:
            async with self.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception as e:
            logger.error(f"Database health check failed: {e}")
            return False
