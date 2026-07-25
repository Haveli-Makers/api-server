import base64
import binascii
import hashlib
import hmac
import json
import secrets
import time
from contextlib import asynccontextmanager
from enum import Enum
from typing import Annotated
from urllib.parse import urlparse

import logfire
import logging
from dotenv import load_dotenv
from pydantic import BaseModel

# Load environment variables early
load_dotenv()

VERSION = "1.0.1"

# Monkey patch save_to_yml to prevent writes to library directory
def patched_save_to_yml(yml_path, cm):
    """Patched version of save_to_yml that prevents writes to library directory"""
    import logging
    logger = logging.getLogger(__name__)
    logger.debug(f"Skipping config write to {yml_path} (patched for API mode)")
    # Do nothing - this prevents the original function from trying to write to the library directory

# Apply the patch before importing hummingbot components
from hummingbot.client.config import config_helpers
config_helpers.save_to_yml = patched_save_to_yml
from database.connection import AsyncDatabaseManager
from config import settings

from hummingbot.core.rate_oracle.rate_oracle import RateOracle, RATE_ORACLE_SOURCES
from hummingbot.core.gateway.gateway_http_client import GatewayHttpClient
from hummingbot.client.config.client_config_map import GatewayConfigMap

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from hummingbot.data_feed.market_data_provider import MarketDataProvider
from hummingbot.client.config.config_crypt import ETHKeyFileSecretManger

from utils.security import BackendAPISecurity
from services.bots_orchestrator import BotsOrchestrator
from services.accounts_service import AccountsService
from services.docker_service import DockerService
from services.gateway_service import GatewayService
from services.unified_connector_service import UnifiedConnectorService
from services.market_data_service import MarketDataService
from services.trading_service import TradingService
from services.executor_service import ExecutorService
from database import AsyncDatabaseManager
from utils.bot_archiver import BotArchiver
from routers import (
    accounts,
    archived_bots,
    backtesting,
    bot_orchestration,
    connectors,
    controllers,
    docker,
    executors,
    gateway,
    gateway_swap,
    gateway_clmm,
    market_data,
    portfolio,
    rate_oracle,
    scripts,
    trading
)

from config import settings


# Set up logging configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# Enable debug logging for MQTT manager
logging.getLogger('services.mqtt_manager').setLevel(logging.DEBUG)


# Get settings from Pydantic Settings
username = settings.security.username
password = settings.security.password
user_username = settings.security.user_username
user_password = settings.security.user_password
debug_mode = settings.security.debug_mode

# Security setup
security = HTTPBearer(auto_error=False)


class UserRole(str, Enum):
    ADMIN = "ADMIN"
    USER = "USER"


class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: UserRole


class AuthenticatedUser(BaseModel):
    username: str
    role: UserRole


def _base64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _base64url_decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def _sign_jwt(unsigned_token: str) -> str:
    if settings.security.jwt_algorithm != "HS256":
        raise ValueError("Only HS256 JWT signing is supported")
    digest = hmac.new(
        settings.security.jwt_secret_key.encode("utf-8"),
        unsigned_token.encode("ascii"),
        hashlib.sha256,
    ).digest()
    return _base64url_encode(digest)


def create_access_token(username: str, role: UserRole) -> str:
    now = int(time.time())
    expires_at = now + settings.security.jwt_access_token_expire_minutes * 60
    header = {"alg": settings.security.jwt_algorithm, "typ": "JWT"}
    payload = {
        "sub": username,
        "role": role.value,
        "iat": now,
        "exp": expires_at,
    }
    encoded_header = _base64url_encode(json.dumps(header, separators=(",", ":")).encode("utf-8"))
    encoded_payload = _base64url_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    unsigned_token = f"{encoded_header}.{encoded_payload}"
    return f"{unsigned_token}.{_sign_jwt(unsigned_token)}"


def decode_access_token(token: str) -> AuthenticatedUser:
    try:
        encoded_header, encoded_payload, signature = token.split(".")
        unsigned_token = f"{encoded_header}.{encoded_payload}"
        expected_signature = _sign_jwt(unsigned_token)
        if not secrets.compare_digest(signature, expected_signature):
            raise ValueError("Invalid token signature")

        header = json.loads(_base64url_decode(encoded_header))
        if header.get("alg") != settings.security.jwt_algorithm:
            raise ValueError("Invalid token algorithm")

        payload = json.loads(_base64url_decode(encoded_payload))
        if int(payload.get("exp", 0)) < int(time.time()):
            raise ValueError("Token has expired")

        return AuthenticatedUser(username=payload["sub"], role=UserRole(payload["role"]))
    except (binascii.Error, KeyError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
            headers={"WWW-Authenticate": "Bearer"},
        )


def authenticate_credentials(login: LoginRequest) -> AuthenticatedUser | None:
    current_username = login.username.encode("utf8")
    current_password = login.password.encode("utf8")

    admin_username_valid = secrets.compare_digest(current_username, f"{username}".encode("utf8"))
    admin_password_valid = secrets.compare_digest(current_password, f"{password}".encode("utf8"))
    if admin_username_valid and admin_password_valid:
        return AuthenticatedUser(username=login.username, role=UserRole.ADMIN)

    if user_username and user_password:
        username_valid = secrets.compare_digest(current_username, user_username.encode("utf8"))
        password_valid = secrets.compare_digest(current_password, user_password.encode("utf8"))
        if username_valid and password_valid:
            return AuthenticatedUser(username=login.username, role=UserRole.USER)

    return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Lifespan context manager for the FastAPI application.
    Handles startup and shutdown events.
    """
    # Ensure password verification file exists
    if BackendAPISecurity.new_password_required():
        # Create secrets manager with CONFIG_PASSWORD
        secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
        BackendAPISecurity.store_password_verification(secrets_manager)
        logging.info("Created password verification file for master_account")

    # =========================================================================
    # 1. Infrastructure Setup
    # =========================================================================

    # Initialize GatewayHttpClient singleton
    parsed_gateway_url = urlparse(settings.gateway.url)
    gateway_config = GatewayConfigMap(
        gateway_api_host=parsed_gateway_url.hostname or "localhost",
        gateway_api_port=str(parsed_gateway_url.port or 15888),
        gateway_use_ssl=parsed_gateway_url.scheme == "https"
    )
    GatewayHttpClient.get_instance(gateway_config)
    logging.info(f"Initialized GatewayHttpClient with URL: {settings.gateway.url}")

    # Initialize secrets manager and database
    secrets_manager = ETHKeyFileSecretManger(password=settings.security.config_password)
    db_manager = AsyncDatabaseManager(settings.database.url)
    await db_manager.create_tables()
    logging.info("Database initialized")

    # Read rate oracle configuration from conf_client.yml
    from utils.file_system import FileSystemUtil
    fs_util = FileSystemUtil()

    try:
        conf_client_path = "credentials/master_account/conf_client.yml"
        config_data = fs_util.read_yaml_file(conf_client_path)

        # Get rate_oracle_source configuration
        rate_oracle_source_data = config_data.get("rate_oracle_source", {})
        source_name = rate_oracle_source_data.get("name", "binance")

        # Get global_token configuration
        global_token_data = config_data.get("global_token", {})
        quote_token = global_token_data.get("global_token_name", "USDT")

        # Create rate source instance
        if source_name in RATE_ORACLE_SOURCES:
            rate_source = RATE_ORACLE_SOURCES[source_name]()
            logging.info(f"Configured RateOracle with source: {source_name}, quote_token: {quote_token}")
        else:
            logging.warning(f"Unknown rate oracle source '{source_name}', defaulting to binance")
            rate_source = RATE_ORACLE_SOURCES["binance"]()
            source_name = "binance"

        # Initialize RateOracle with configured source and quote token
        rate_oracle = RateOracle.get_instance()
        rate_oracle.source = rate_source
        rate_oracle.quote_token = quote_token

    except FileNotFoundError:
        logging.warning("conf_client.yml not found, using default RateOracle configuration (binance, USDT)")
        rate_oracle = RateOracle.get_instance()
    except Exception as e:
        logging.warning(f"Error reading conf_client.yml: {e}, using default RateOracle configuration")
        rate_oracle = RateOracle.get_instance()

    # =========================================================================
    # 2. UnifiedConnectorService - Single source of truth for all connectors
    # =========================================================================

    connector_service = UnifiedConnectorService(
        secrets_manager=secrets_manager,
        db_manager=db_manager
    )
    logging.info("UnifiedConnectorService initialized")

    # =========================================================================
    # 3. Services that depend on connector_service
    # =========================================================================

    # MarketDataService - candles, order books, spreads, prices
    market_data_service = MarketDataService(
        connector_service=connector_service,
        rate_oracle=rate_oracle,
        db_manager=db_manager,
        cleanup_interval=settings.market_data.cleanup_interval,
        feed_timeout=settings.market_data.feed_timeout
    )
    logging.info("MarketDataService initialized")

    # TradingService - order placement, positions, trading interfaces
    trading_service = TradingService(
        connector_service=connector_service,
        market_data_service=market_data_service
    )
    logging.info("TradingService initialized")

    # AccountsService - account management, balances, portfolio (simplified)
    accounts_service = AccountsService(
        account_update_interval=settings.app.account_update_interval,
        gateway_url=settings.gateway.url,
        db_manager=db_manager
    )
    # Inject services into AccountsService
    accounts_service._connector_service = connector_service
    accounts_service._market_data_service = market_data_service
    accounts_service._trading_service = trading_service
    logging.info("AccountsService initialized")

    # =========================================================================
    # 4. ExecutorService - depends on TradingService (NO circular dependency)
    # =========================================================================

    executor_service = ExecutorService(
        trading_service=trading_service,
        db_manager=db_manager,
        default_account="master_account",
        update_interval=1.0,
        max_retries=10
    )
    logging.info("ExecutorService initialized")

    # =========================================================================
    # 5. Other Services
    # =========================================================================

    bots_orchestrator = BotsOrchestrator(
        broker_host=settings.broker.host,
        broker_port=settings.broker.port,
        broker_username=settings.broker.username,
        broker_password=settings.broker.password
    )

    docker_service = DockerService()
    gateway_service = GatewayService()
    bot_archiver = BotArchiver(
        settings.aws.api_key,
        settings.aws.secret_key,
        settings.aws.s3_default_bucket_name
    )

    # Initialize database
    await db_manager.ensure_initialized()
    # =========================================================================
    # 6. Store services in app state
    # =========================================================================

    app.state.db_manager = db_manager
    app.state.connector_service = connector_service
    app.state.market_data_service = market_data_service
    app.state.trading_service = trading_service
    app.state.accounts_service = accounts_service
    app.state.executor_service = executor_service
    app.state.bots_orchestrator = bots_orchestrator
    app.state.docker_service = docker_service
    app.state.gateway_service = gateway_service
    app.state.bot_archiver = bot_archiver

    # =========================================================================
    # 7. Start services
    # =========================================================================

    # Initialize all trading connectors FIRST (before any service that might use them)
    # This ensures OrdersRecorder is properly attached before any concurrent access
    logging.info("Initializing all trading connectors...")
    await connector_service.initialize_all_trading_connectors()

    bots_orchestrator.start()
    accounts_service.start()
    market_data_service.start()
    executor_service.start()
    await executor_service.recover_positions_from_db()

    logging.info("All services started successfully")

    yield

    # =========================================================================
    # Shutdown services
    # =========================================================================

    logging.info("Shutting down services...")

    bots_orchestrator.stop()
    await accounts_service.stop()
    await executor_service.stop()
    market_data_service.stop()
    await connector_service.stop_all()
    docker_service.cleanup()
    await db_manager.close()

    logging.info("All services stopped")


# Initialize FastAPI with metadata and lifespan
app = FastAPI(
    title="Hummingbot API",
    description="API for managing Hummingbot trading instances",
    version=VERSION,
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Modify in production to specific origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """
    Custom handler for validation errors to log detailed error messages.
    """
    # Build a readable error message from validation errors
    error_messages = []
    for error in exc.errors():
        loc = " -> ".join(str(l) for l in error.get("loc", []))
        msg = error.get("msg", "Validation error")
        error_messages.append(f"{loc}: {msg}")

    # Log the validation error with details
    logging.warning(
        f"Validation error on {request.method} {request.url.path}: {'; '.join(error_messages)}"
    )

    # Return standard FastAPI validation error response
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )


logfire.configure(send_to_logfire="if-token-present", environment=settings.app.logfire_environment, service_name="hummingbot-api")
logfire.instrument_fastapi(app)

@app.post("/auth/token", response_model=TokenResponse, tags=["Auth"])
async def login(login_request: LoginRequest):
    """Authenticate credentials and return a JWT access token."""
    authenticated_user = authenticate_credentials(login_request)
    if authenticated_user is None and not debug_mode:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect username or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if authenticated_user is None:
        authenticated_user = AuthenticatedUser(username=login_request.username, role=UserRole.ADMIN)

    access_token = create_access_token(authenticated_user.username, authenticated_user.role)
    return TokenResponse(
        access_token=access_token,
        expires_in=settings.security.jwt_access_token_expire_minutes * 60,
        role=authenticated_user.role,
    )


def get_current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(security)],
) -> AuthenticatedUser:
    """Authenticate user using a JWT Bearer token."""
    if debug_mode:
        return AuthenticatedUser(username="debug", role=UserRole.ADMIN)

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    return decode_access_token(credentials.credentials)


def require_user(current_user: Annotated[AuthenticatedUser, Depends(get_current_user)]) -> AuthenticatedUser:
    return current_user


def require_admin(current_user: Annotated[AuthenticatedUser, Depends(get_current_user)]) -> AuthenticatedUser:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin role required",
        )
    return current_user


@app.get("/auth/me", response_model=AuthenticatedUser, tags=["Auth"])
async def get_me(current_user: Annotated[AuthenticatedUser, Depends(get_current_user)]):
    return current_user

# Include all routers with authentication
app.include_router(docker.router, dependencies=[Depends(require_user)])
app.include_router(gateway.router, dependencies=[Depends(require_user)])
app.include_router(accounts.router, dependencies=[Depends(require_user)])
app.include_router(connectors.router, dependencies=[Depends(require_user)])
app.include_router(portfolio.router, dependencies=[Depends(require_user)])
app.include_router(trading.router, dependencies=[Depends(require_user)])
app.include_router(gateway_swap.router, dependencies=[Depends(require_user)])
app.include_router(gateway_clmm.router, dependencies=[Depends(require_user)])
app.include_router(bot_orchestration.router, dependencies=[Depends(require_user)])
app.include_router(controllers.router, dependencies=[Depends(require_user)])
app.include_router(scripts.router, dependencies=[Depends(require_user)])
app.include_router(market_data.router, dependencies=[Depends(require_user)])
app.include_router(rate_oracle.router, dependencies=[Depends(require_user)])
app.include_router(backtesting.router, dependencies=[Depends(require_user)])
app.include_router(archived_bots.router, dependencies=[Depends(require_user)])
app.include_router(executors.router, dependencies=[Depends(require_user)])

@app.get("/")
async def root():
    """API root endpoint returning basic information."""
    return {
        "name": "Hummingbot API",
        "version": VERSION,
        "status": "running",
    }
