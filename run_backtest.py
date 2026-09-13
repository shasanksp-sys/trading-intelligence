"""
CLI entry point for the backtest engine (engine/backtest/).

Usage:
    python3 run_backtest.py --symbols AAPL --start 2020-01-01 --end 2024-01-01

    # a different strategy class, once you've written one:
    python3 run_backtest.py --strategy engine.backtest.strategies.my_strategy.MyStrategy \\
        --symbols AAPL,MSFT --start 2020-01-01 --end 2024-01-01

Defaults to the reference SmaCrossoverStrategy against Yahoo Finance daily
data -- swap --strategy once a real course-derived strategy is coded up
(see engine/backtest/strategies/example_sma_crossover.py's docstring for
why none of the current course strategies are ready yet).
"""

import argparse
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "engine"))

from backtest.data_feed import YFinanceDataFeed
from backtest.engine import BacktestEngine
from backtest.metrics import compute_metrics, trade_log
from backtest.strategies.example_sma_crossover import SmaCrossoverStrategy


def _load_strategy_class(dotted_path: str):
    module_path, class_name = dotted_path.rsplit(".", 1)
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def run(symbols, start, end, strategy_cls, starting_cash):
    engine = BacktestEngine(
        data_feed=YFinanceDataFeed(),
        symbols=symbols,
        start=start,
        end=end,
        strategy_cls=strategy_cls,
        starting_cash=starting_cash,
    )
    result = engine.run()

    print(f"\nStrategy: {result.strategy_name}  |  Symbols: {result.symbols}")
    print(f"Period: {start} to {end}  |  Starting cash: {starting_cash:,.2f}\n")

    metrics = compute_metrics(result)
    for key, value in metrics.items():
        print(f"  {key}: {value}")

    log = trade_log(result)
    if not log.empty:
        print(f"\nTrade log ({len(log)} trades):")
        print(log.to_string(index=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default="AAPL", help="Comma-separated symbols, e.g. AAPL,MSFT")
    parser.add_argument("--start", default="2020-01-01")
    parser.add_argument("--end", default="2024-01-01")
    parser.add_argument("--cash", type=float, default=100_000.0)
    parser.add_argument("--strategy", default=None, help="Dotted path to a Strategy subclass")
    args = parser.parse_args()

    strategy_cls = _load_strategy_class(args.strategy) if args.strategy else SmaCrossoverStrategy
    run(args.symbols.split(","), args.start, args.end, strategy_cls, args.cash)
