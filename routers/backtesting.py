import asyncio
import os
import sys

from fastapi import APIRouter, HTTPException
from hummingbot.data_feed.candles_feed.candles_factory import CandlesFactory
from hummingbot.strategy_v2.backtesting.backtesting_engine_base import BacktestingEngineBase

import hummingbot
_hb_root = os.path.dirname(os.path.dirname(hummingbot.__file__))
if _hb_root not in sys.path:
    sys.path.insert(0, _hb_root)

from config import settings
from models.backtesting import BacktestingConfig, ControllerOrderbookBacktestConfig

router = APIRouter(tags=["Backtesting"], prefix="/backtesting")
candles_factory = CandlesFactory()
backtesting_engine = BacktestingEngineBase()


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


@router.post("/run-controller-orderbook-backtest")
async def run_controller_orderbook_backtest(config: ControllerOrderbookBacktestConfig):
    """
    Run any strategy_v2 controller against recorded orderbook + trade data.
    """
    try:
        from hummingbot.strategy_v2.backtesting.orderbook_backtesting_engine import OrderbookBacktestingEngine

        engine = OrderbookBacktestingEngine()

        if isinstance(config.config, str):
            controller_config = backtesting_engine.get_controller_config_instance_from_yml(
                config_path=config.config,
                controllers_conf_dir_path=settings.app.controllers_path,
                controllers_module=settings.app.controllers_module,
            )
        else:
            controller_config = backtesting_engine.get_controller_config_instance_from_dict(
                config_data=config.config,
                controllers_module=settings.app.controllers_module,
            )

        await engine.load_candles(controller_config, config.connector, config.pair, config.start, config.end)

        result = await asyncio.to_thread(
            _run_controller_orderbook_backtest_sync,
            engine, controller_config, config,
        )
        return result
    except HTTPException:
        raise
    except (NotImplementedError, ValueError, FileNotFoundError) as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


def _run_controller_orderbook_backtest_sync(engine, controller_config, config):
    import math

    loop = asyncio.new_event_loop()
    try:
        result = loop.run_until_complete(engine.run_backtesting(
            controller_config=controller_config,
            connector=config.connector,
            pair=config.pair,
            start=config.start,
            end=config.end,
            maker_fee=config.maker_fee,
            taker_fee=config.taker_fee,
            latency_ms=config.latency_ms,
            fill_at_touch=config.fill_at_touch,
        ))
    finally:
        loop.close()

    def _clean(records):
        for rec in records:
            for k, v in rec.items():
                if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                    rec[k] = None
                elif hasattr(v, "isoformat"):
                    rec[k] = v.isoformat()
        return records

    executors_info = [e.to_dict() for e in result["executors"]]
    fills = _clean(result["fills"]) if result["fills"] else []
    equity_data = result["equity"]
    equity = []
    if equity_data:
        import pandas as _pd
        equity = _clean(_pd.DataFrame(equity_data, columns=["ts", "pnl", "position"]).to_dict(orient="records"))

    results = _clean([dict(result["results"])])[0]

    return {
        "executors": executors_info,
        "results": results,
        "strategy": result.get("strategy"),
        "fills": fills,
        "equity": equity,
        "fees": result["fees"],
        "final_position": result["final_position"],
        "reconciliation": result.get("reconciliation"),
        "tick_size": result.get("tick_size"),
        "candles": result.get("candles") or [],
    }
