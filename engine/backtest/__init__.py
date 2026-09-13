"""
Backtest engine -- separate from the extraction/AI pipeline in
engine/pipeline and engine/processors, but sharing this codebase so a
strategy's approved spec (from db.strategies / 06_FINAL_STRATEGIES) can
eventually feed straight into a Strategy subclass here.

Quick map of the pieces:
  types.py     -- Order/Fill/Position/Trade/Bar: the shared vocabulary
  data_feed.py -- historical OHLCV sources (CSV, yfinance, ...)
  broker.py    -- fill simulation + portfolio bookkeeping (the seam a
                  future live broker adapter would also implement)
  strategy.py  -- Strategy base class: subclass this per strategy
  engine.py    -- BacktestEngine: wires the above together, runs the loop
  metrics.py   -- turns a BacktestResult into win rate / drawdown / etc.

See engine/backtest/strategies/example_sma_crossover.py for a minimal
worked example, and run_backtest.py (project root) for a CLI entry point.
"""

from .broker import Broker, SimulatedBroker
from .data_feed import CSVDataFeed, DataFeed, YFinanceDataFeed
from .engine import BacktestEngine, BacktestResult
from .spot_analysis import check_stop_target_realism, compute_spot_stats, print_spot_report
from .strategy import Strategy
from .types import CommissionModel, OrderSide, OrderType, SlippageModel

__all__ = [
    "Broker", "SimulatedBroker",
    "CSVDataFeed", "DataFeed", "YFinanceDataFeed",
    "BacktestEngine", "BacktestResult",
    "compute_spot_stats", "print_spot_report", "check_stop_target_realism",
    "Strategy",
    "CommissionModel", "OrderSide", "OrderType", "SlippageModel",
]
