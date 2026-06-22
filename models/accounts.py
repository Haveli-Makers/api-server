from typing import Any, Dict

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
    encrypted: bool = Field(
        default=False,
        description=(
            "If True, all string values in credentials are RSA-OAEP encrypted and base64-encoded. "
        ),
    )


class CredentialDetailsResponse(BaseModel):
    """Response model for connector credential details."""
    connector_name: str = Field(description="Connector name")
    parameters: Dict[str, Any] = Field(description="Masked connector credential parameters")