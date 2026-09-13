"""
Reference strategy: long-only fast/slow SMA crossover.

This exists to prove the engine works end-to-end and to show the
pattern for writing a real one -- it is NOT either of the two course-
extracted strategies (those still have open questions on exit_conditions
/ target per 05_REVIEW/review_report.json and shouldn't be coded up
until that's resolved, see PROJECT_SUMMARY.md and the project's
evidence-before-rules principle).

To turn a resolved strategy spec into one of these: `params` takes
whatever numeric levels the spec pins down (indicator lengths, stop/
target distances), and on_bar's body is where the spec's entry_conditions
/ exit_conditions text becomes an actual boolean expression over `history`.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


class SmaCrossoverStrategy(Strategy):
    def __init__(self, symbols: list[str], fast: int = 10, slow: int = 30,
                 stop_pct: float = 0.02, target_pct: float = 0.04, **params):
        super().__init__(symbols, **params)
        self.fast = fast
        self.slow = slow
        self.stop_pct = stop_pct
        self.target_pct = target_pct

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < self.slow + 1:
                continue  # not enough history yet to compute the slow average

            fast_ma = df["close"].rolling(self.fast).mean()
            slow_ma = df["close"].rolling(self.slow).mean()

            crossed_up = fast_ma.iloc[-2] <= slow_ma.iloc[-2] and fast_ma.iloc[-1] > slow_ma.iloc[-1]
            crossed_down = fast_ma.iloc[-2] >= slow_ma.iloc[-2] and fast_ma.iloc[-1] < slow_ma.iloc[-1]

            position = broker.get_position(symbol)
            price = df["close"].iloc[-1]

            if not position.is_open and crossed_up:
                broker.buy(
                    symbol, quantity=self._position_size(broker, price),
                    stop_price=price * (1 - self.stop_pct),
                    target_price=price * (1 + self.target_pct),
                )
            elif position.is_open and crossed_down:
                broker.close_position(symbol, tag="signal_reversal")

    @staticmethod
    def _position_size(broker: Broker, price: float) -> float:
        """Fixed-fractional sizing: risk 10% of current cash per entry -- swap for your own rule."""
        return (broker.get_cash() * 0.10) // price
