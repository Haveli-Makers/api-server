from typing import Any, Dict, Optional

from pydantic import BaseModel, Field


class LeverageRequest(BaseModel):
    """Request model for setting leverage on perpetual connectors"""
    trading_pair: str = Field(description="Trading pair (e.g., BTC-USDT)")
    leverage: int = Field(description="Leverage value (typically 1-125)", ge=1, le=125)


class PositionModeRequest(BaseModel):
    """Request model for setting position mode on perpetual connectors"""
    position_mode: str = Field(description="Position mode (HEDGE or ONEWAY)")


class CredentialRequest(BaseModel):
    """Request model for adding connector credentials"""
    credentials: Dict[str, Any] = Field(description="Connector credentials dictionary")


class CredentialDetailsResponse(BaseModel):
    """Response model for connector credential details."""
    connector_name: str = Field(description="Connector name")
    parameters: Dict[str, Any] = Field(description="Masked connector credential parameters")
    alias: Optional[str] = Field(default=None, description="Optional alias for the connector credentials")
    credential_type: str = Field(description="Type of the connector credentials (e.g., 'master', 'sub account')")