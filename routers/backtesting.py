import asyncio
import os
import sys
from typing import Dict, List

from fastapi import APIRouter, HTTPException
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

import hummingbot
_hb_root = os.path.dirname(os.path.dirname(hummingbot.__file__))
if _hb_root not in sys.path:
    sys.path.insert(0, _hb_root)

from backtest_engine import Backtest
from backtest_registry import ensure_builtins, registry, get_strategy, list_strategies
from config import settings
from models.backtesting import BacktestingConfig, OrderbookBacktestConfig

router = APIRouter(tags=["Backtesting"], prefix="/backtesting")
candles_factory = CandlesFactory()
backtesting_engine = BacktestingEngineBase()


@router.get("/orderbook-strategies")
async def list_orderbook_strategies() -> List[Dict]:
    ensure_builtins()
    return list_strategies()


def _run_orderbook_backtest_sync(config: OrderbookBacktestConfig):
    """Run in a thread since the backtest engine is CPU-bound and synchronous."""
    ensure_builtins()

    strategy_args = dict(config.strategy_args or {})
    strategy_args["tick_size"] = strategy_args.get("tick_size", 0.01)

    strategy = get_strategy(config.strategy, strategy_args)

    bt = Backtest(config.connector, config.pair, config.start, config.end,
                  config.maker_fee, config.taker_fee, config.latency_ms, config.fill_at_touch)
    result = bt.run(strategy)
    report_df = strategy.report(bt)

    import math

    def _clean(records):
        for rec in records:
            for k, v in rec.items():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    rec[k] = None
                elif hasattr(v, 'isoformat'):
                    rec[k] = v.isoformat()
        return records

    fills = _clean(result.fills.to_dict(orient="records")) if not result.fills.empty else []
    equity = _clean(result.equity.to_dict(orient="records")) if not result.equity.empty else []
    strategy_report = _clean(report_df.fillna(0).to_dict(orient="records")) if not report_df.empty else []

    return {
        "fills": fills,
        "equity": equity,
        "summary": result.summary(),
        "strategy_report": strategy_report,
        "strategy_summary": strategy.summarize(report_df),
        "fees": result.fees,
        "final_position": result.final_position,
    }


@router.post("/run-orderbook-backtest")
async def run_orderbook_backtest(config: OrderbookBacktestConfig):
    try:
        return await asyncio.to_thread(_run_orderbook_backtest_sync, config)
    except KeyError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/run-backtesting")
async def run_backtesting(backtesting_config: BacktestingConfig):
    """
    Run a backtesting simulation with the provided configuration.

    Args:
        backtesting_config: Configuration for the backtesting including start/end time,
                          resolution, trade cost, and controller config

    Returns:
        Dictionary containing executors, processed data, and results from the backtest

    Raises:
        Returns error dictionary if backtesting fails
    """
    try:
        if isinstance(backtesting_config.config, str):
            controller_config = backtesting_engine.get_controller_config_instance_from_yml(
                config_path=backtesting_config.config,
                controllers_conf_dir_path=settings.app.controllers_path,
                controllers_module=settings.app.controllers_module
            )
        else:
            controller_config = backtesting_engine.get_controller_config_instance_from_dict(
                config_data=backtesting_config.config,
                controllers_module=settings.app.controllers_module
            )
        backtesting_results = await backtesting_engine.run_backtesting(
            controller_config=controller_config, trade_cost=backtesting_config.trade_cost,
            start=int(backtesting_config.start_time), end=int(backtesting_config.end_time),
            backtesting_resolution=backtesting_config.backtesting_resolution)
        processed_data = backtesting_results["processed_data"]["features"].fillna(0)
        executors_info = [e.to_dict() for e in backtesting_results["executors"]]
        backtesting_results["processed_data"] = processed_data.to_dict()
        results = backtesting_results["results"]
        results["sharpe_ratio"] = results["sharpe_ratio"] if results["sharpe_ratio"] is not None else 0
        return {
            "executors": executors_info,
            "processed_data": backtesting_results["processed_data"],
            "results": backtesting_results["results"],
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
