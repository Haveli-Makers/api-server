from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field


# Script file operations
class Script(BaseModel):
    """Script file content"""
    content: str = Field(description="Script source code")


class ScriptResponse(BaseModel):
    """Response for getting a script"""
    name: str = Field(description="Script name")
    content: str = Field(description="Script source code")


# Script configuration operations
class ScriptConfig(BaseModel):
    """Script configuration content"""
    config_name: str = Field(description="Configuration name")
    script_file_name: str = Field(description="Script file name")
    controllers_config: List[str] = Field(default=[], description="List of controller configurations")
    candles_config: List[Dict[str, Any]] = Field(default=[], description="Candles configuration")
    markets: Dict[str, Any] = Field(default={}, description="Markets configuration")


class ScriptConfigResponse(BaseModel):
    """Response for script configuration with metadata"""
    config_name: str = Field(description="Configuration name")
    script_file_name: str = Field(description="Script file name")
    controllers_config: List[str] = Field(default=[], description="List of controller configurations")
    candles_config: List[Dict[str, Any]] = Field(default=[], description="Candles configuration")
    markets: Dict[str, Any] = Field(default={}, description="Markets configuration")
    error: Optional[str] = Field(None, description="Error message if config is malformed")


SUPPORTED_SPREAD_CAPTURE_CONNECTORS = [
    "binance", "binance_perpetual", "binance_us", "kucoin", "gate_io",
    "mexc", "ascend_ex", "cube", "hyperliquid", "dexalot", "coindcx",
    "wazirx", "coinswitch",
]


class SpreadCaptureRunRequest(BaseModel):
    """Request body to configure the spread_capture script"""
    connector_name: str = Field(
        default="binance",
        description=f"Exchange connector to use. Supported: {', '.join(SUPPORTED_SPREAD_CAPTURE_CONNECTORS)}",
    )
    quote_token: str = Field(
        default="USDT",
        description="Quote token to filter trading pairs (e.g. USDT, USDC)",
    )
    interval_sec: int = Field(
        default=900,
        gt=0,
        description="Fetch interval in seconds (e.g. 900 for 15 minutes)",
    )
    excluding_pairs: str = Field(
        default="",
        description="Comma-separated trading pairs to exclude (e.g. BTC-USDT,ETH-USDT), leave empty to include all",
    )
    data_retention_days: int = Field(
        default=30,
        ge=0,
        description="Number of days to retain market data records (0 to keep all data)",
    )