import logging
from typing import Dict, List

from hummingbot.core.volume_oracle.volume_oracle import VolumeOracle, VOLUME_ORACLE_SOURCES

logger = logging.getLogger(__name__)


async def get_24h_volume(exchange: str, trading_pair: str) -> Dict[str, object]:
    """
    Fetch 24h volume for a trading pair using the VolumeOracle.

    :param exchange: Exchange name (e.g. "binance", "okx")
    :param trading_pair: Trading pair in HB format e.g. "BTC-USDT"
    :return: dict with exchange, trading_pair, symbol, base_volume, last_price, quote_volume
    """
    source = VolumeOracle.source_for_exchange(exchange)
    oracle = VolumeOracle(source=source)
    try:
        result = await oracle.get_24h_volume(trading_pair)
        return {
            "exchange": result["exchange"],
            "trading_pair": result["trading_pair"],
            "symbol": result["symbol"],
            "base_volume": float(result["base_volume"]),
            "last_price": float(result["last_price"]),
            "quote_volume": float(result.get("quote_volume", 0)),
        }
    finally:
        await oracle.close()


def get_supported_exchanges() -> List[str]:
    """Return exchange names supported by the VolumeOracle."""
    return sorted(VOLUME_ORACLE_SOURCES.keys())
