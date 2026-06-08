import json
import time
import yaml
from typing import Dict, List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from starlette import status

from database.connection import AsyncDatabaseManager
from database.models import MarketData
from deps import get_database_manager
from models import Script, ScriptConfig, SpreadCaptureRunRequest
from utils.file_system import fs_util

_SUPPORTED_CONNECTORS = [
    "binance", "binance_perpetual", "binance_us", "kucoin", "gate_io",
    "mexc", "ascend_ex", "cube", "hyperliquid", "dexalot", "coindcx",
    "wazirx", "coinswitch",
]


def _get_rate_source(connector_name: str):
    """Return the appropriate RateSource instance for the given connector."""
    name = connector_name.lower()
    if name == "binance":
        from hummingbot.core.rate_oracle.sources.binance_rate_source import BinanceRateSource
        return BinanceRateSource()
    elif name == "binance_perpetual":
        from hummingbot.core.rate_oracle.sources.binance_rate_source import BinanceRateSource
        return BinanceRateSource()
    elif name == "binance_us":
        from hummingbot.core.rate_oracle.sources.binance_us_rate_source import BinanceUSRateSource
        return BinanceUSRateSource()
    elif name == "kucoin":
        from hummingbot.core.rate_oracle.sources.kucoin_rate_source import KucoinRateSource
        return KucoinRateSource()
    elif name == "gate_io":
        from hummingbot.core.rate_oracle.sources.gate_io_rate_source import GateIoRateSource
        return GateIoRateSource()
    elif name == "mexc":
        from hummingbot.core.rate_oracle.sources.mexc_rate_source import MexcRateSource
        return MexcRateSource()
    elif name == "ascend_ex":
        from hummingbot.core.rate_oracle.sources.ascend_ex_rate_source import AscendExRateSource
        return AscendExRateSource()
    elif name == "cube":
        from hummingbot.core.rate_oracle.sources.cube_rate_source import CubeRateSource
        return CubeRateSource()
    elif name == "hyperliquid":
        from hummingbot.core.rate_oracle.sources.hyperliquid_rate_source import HyperliquidRateSource
        return HyperliquidRateSource()
    elif name == "dexalot":
        from hummingbot.core.rate_oracle.sources.dexalot_rate_source import DexalotRateSource
        return DexalotRateSource()
    elif name == "wazirx":
        from hummingbot.core.rate_oracle.sources.wazirx_rate_source import WazirxRateSource
        return WazirxRateSource()
    elif name == "coindcx":
        from hummingbot.core.rate_oracle.sources.coindcx_rate_source import CoindcxRateSource
        return CoindcxRateSource()
    elif name == "coinswitch":
        from hummingbot.core.rate_oracle.sources.coinswitch_rate_source import CoinswitchRateSource
        return CoinswitchRateSource()
    else:
        raise ValueError(
            f"Unsupported connector: {connector_name}. "
            f"Supported: {', '.join(_SUPPORTED_CONNECTORS)}"
        )


router = APIRouter(tags=["Scripts"], prefix="/scripts")


@router.get("/", response_model=List[str])
async def list_scripts():
    """
    List all available scripts.
    
    Returns:
        List of script names (without .py extension)
    """
    return [f.replace('.py', '') for f in fs_util.list_files('scripts') if f.endswith('.py')]


@router.post("/spread-capture/run")
async def run_spread_capture(
    request: SpreadCaptureRunRequest,
    db_manager: AsyncDatabaseManager = Depends(get_database_manager),
):
    """
    Replicates what spread_capture does on each tick:
    1. Fetches live bid/ask prices from the exchange rate source.
    2. Stores the results in the MarketData table (with timestamp).
    3. Applies data-retention cleanup (same as the script).
    4. Returns the stored rows so you see timestamp + spread data.
    """
    config_data = {
        "script_file_name": "spread_capture",
        "connector_name": request.connector_name,
        "quote_token": request.quote_token,
        "interval_sec": request.interval_sec,
        "excluding_pairs": request.excluding_pairs,
        "data_retention_days": request.data_retention_days,
    }
    try:
        yaml_content = yaml.dump(config_data, default_flow_style=False)
        fs_util.add_file("conf/scripts", "spread_capture.yml", yaml_content, override=True)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to save spread_capture config: {e}")

    try:
        rate_source = _get_rate_source(request.connector_name)
        bid_ask_prices = await rate_source.get_bid_ask_prices(quote_token=request.quote_token)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to fetch spread data: {e}")

    excluding = (
        {p.strip().upper() for p in request.excluding_pairs.split(",") if p.strip()}
        if request.excluding_pairs
        else set()
    )

    now_ts = int(time.time())

    rows = [
        {
            "timestamp": now_ts,
            "exchange": request.connector_name,
            "trading_pair": pair,
            "best_bid": float(prices["bid"]),
            "best_ask": float(prices["ask"]),
            "mid_price": float(prices["mid"]),
            "spread": round(float(prices["spread"]), 2),
        }
        for pair, prices in bid_ask_prices.items()
        if pair not in excluding
    ]

    if not rows:
        raise HTTPException(status_code=404, detail="No market data returned for the given config")

    try:
        async with db_manager.get_session_context() as session:
            await session.execute(
                pg_insert(MarketData).values(rows).on_conflict_do_nothing()
            )
            if request.data_retention_days > 0:
                cutoff = now_ts - request.data_retention_days * 24 * 3600
                await session.execute(
                    delete(MarketData).where(
                        MarketData.exchange == request.connector_name,
                        MarketData.timestamp < cutoff,
                    )
                )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to store market data: {e}")

    return {
        "status": "success",
        "config": config_data,
        "data": rows,
    }


# Script Configuration endpoints (must come before script name routes)
@router.get("/configs/", response_model=List[Dict])
async def list_script_configs():
    """
    List all script configurations with metadata.
    
    Returns:
        List of script configuration objects with name, script_file_name, and other metadata
    """
    try:
        config_files = [f for f in fs_util.list_files('conf/scripts') if f.endswith('.yml')]
        configs = []
        
        for config_file in config_files:
            config_name = config_file.replace('.yml', '')
            try:
                config = fs_util.read_yaml_file(f"conf/scripts/{config_file}")
                configs.append({
                    "config_name": config_name,
                    "script_file_name": config.get("script_file_name", "unknown"),
                    "controllers_config": config.get("controllers_config", []),
                    "candles_config": config.get("candles_config", []),
                    "markets": config.get("markets", {})
                })
            except Exception as e:
                # If config is malformed, still include it with basic info
                configs.append({
                    "config_name": config_name,
                    "script_file_name": "error",
                    "error": str(e)
                })
        
        return configs
    except FileNotFoundError:
        return []


@router.get("/configs/{config_name}", response_model=Dict)
async def get_script_config(config_name: str):
    """
    Get script configuration by config name.
    
    Args:
        config_name: Name of the configuration file to retrieve
        
    Returns:
        Dictionary with script configuration
        
    Raises:
        HTTPException: 404 if configuration not found
    """
    try:
        config = fs_util.read_yaml_file(f"conf/scripts/{config_name}.yml")
        return config
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")


@router.post("/configs/{config_name}", status_code=status.HTTP_201_CREATED)
async def create_or_update_script_config(config_name: str, config: Dict):
    """
    Create or update script configuration.
    
    Args:
        config_name: Name of the configuration file
        config: Configuration dictionary to save
        
    Returns:
        Success message when configuration is saved
        
    Raises:
        HTTPException: 400 if save error occurs
    """
    try:
        yaml_content = yaml.dump(config, default_flow_style=False)
        fs_util.add_file('conf/scripts', f"{config_name}.yml", yaml_content, override=True)
        return {"message": f"Configuration '{config_name}' saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/configs/{config_name}")
async def delete_script_config(config_name: str):
    """
    Delete script configuration.
    
    Args:
        config_name: Name of the configuration file to delete
        
    Returns:
        Success message when configuration is deleted
        
    Raises:
        HTTPException: 404 if configuration not found
    """
    try:
        fs_util.delete_file('conf/scripts', f"{config_name}.yml")
        return {"message": f"Configuration '{config_name}' deleted successfully"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Configuration '{config_name}' not found")


@router.get("/{script_name}", response_model=Dict[str, str])
async def get_script(script_name: str):
    """
    Get script content by name.
    
    Args:
        script_name: Name of the script to retrieve
        
    Returns:
        Dictionary with script name and content
        
    Raises:
        HTTPException: 404 if script not found
    """
    try:
        content = fs_util.read_file(f"scripts/{script_name}.py")
        return {
            "name": script_name,
            "content": content
        }
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Script '{script_name}' not found")


@router.post("/{script_name}", status_code=status.HTTP_201_CREATED)
async def create_or_update_script(script_name: str, script: Script):
    """
    Create or update a script.
    
    Args:
        script_name: Name of the script (from URL path)
        script: Script object with content
        
    Returns:
        Success message when script is saved
        
    Raises:
        HTTPException: 400 if save error occurs
    """
    try:
        fs_util.add_file('scripts', f"{script_name}.py", script.content, override=True)
        return {"message": f"Script '{script_name}' saved successfully"}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete("/{script_name}")
async def delete_script(script_name: str):
    """
    Delete a script.
    
    Args:
        script_name: Name of the script to delete
        
    Returns:
        Success message when script is deleted
        
    Raises:
        HTTPException: 404 if script not found
    """
    try:
        fs_util.delete_file('scripts', f"{script_name}.py")
        return {"message": f"Script '{script_name}' deleted successfully"}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Script '{script_name}' not found")


@router.get("/{script_name}/config/template", response_model=Dict)
async def get_script_config_template(script_name: str):
    """
    Get script configuration template with default values.
    
    Args:
        script_name: Name of the script to get template for
        
    Returns:
        Dictionary with configuration template and default values
        
    Raises:
        HTTPException: 404 if script configuration class not found
    """
    config_class = fs_util.load_script_config_class(script_name)
    if config_class is None:
        raise HTTPException(status_code=404, detail=f"Script configuration class for '{script_name}' not found")

    # Extract fields and default values
    config_fields = {name: field.default for name, field in config_class.model_fields.items()}
    return json.loads(json.dumps(config_fields, default=str))