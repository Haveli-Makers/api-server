from typing import Dict, Optional, Union
from pydantic import BaseModel


class BacktestingConfig(BaseModel):
    start_time: int = 1735689600  # 2025-01-01 00:00:00
    end_time: int = 1738368000  # 2025-02-01 00:00:00
    backtesting_resolution: str = "1m"
    trade_cost: float = 0.0006
    config: Union[Dict, str]


class ControllerOrderbookBacktestConfig(BaseModel):
    """Run any strategy_v2 controller against recorded orderbook + trade data."""
    connector: str
    pair: str
    start: str
    end: Optional[str] = None
    maker_fee: float = 0.001
    taker_fee: float = 0.001
    latency_ms: float = 100
    fill_at_touch: bool = False
    config: Union[Dict, str]