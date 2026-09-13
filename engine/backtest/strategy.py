"""
Base class every strategy -- of any shape -- subclasses.

Deliberately minimal: on_bar() hands the strategy the full historical
window up to and including "now" (a plain pandas DataFrame per symbol,
so any indicator -- moving averages, RSI, a hand-coded candle pattern,
whatever a course actually described) and a Broker to act through. The
engine guarantees the window never includes future bars, so look-ahead
bias is prevented structurally rather than left to each strategy author
to remember.

This is the extensibility point that keeps "no blockage for any future
strategy" true: since on_bar receives raw OHLCV history and an
unrestricted Python method body, anything expressible as "a function of
price history so far" -- which is the actual, unavoidable definition of
what's backtestable at all -- fits here. Multi-symbol strategies (pairs
trading, spreads) just read multiple entries out of the `history` dict
in one on_bar call.
"""

from __future__ import annotations

from abc import ABC
from datetime import datetime

import pandas as pd

from .broker import Broker


class Strategy(ABC):
    def __init__(self, symbols: list[str], **params):
        self.symbols = symbols
        self.params = params

    def on_start(self, broker: Broker) -> None:
        """Optional one-time setup hook, called before the first bar."""

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        """
        Called once per timestamp present in any symbol's data.

        history[symbol] is that symbol's OHLCV DataFrame sliced to every
        bar up to and including `timestamp` -- never further. A symbol
        with no bar at this exact timestamp (e.g. mismatched trading
        calendars) is simply absent from the dict for this call.

        Act by calling methods on `broker` (buy/sell/set_exit_orders/
        close_position/get_position/get_cash) -- never mutate `history`
        or assume anything about execution timing beyond what broker.py
        documents.
        """
        raise NotImplementedError

    def on_end(self, broker: Broker) -> None:
        """Optional teardown hook, called after the last bar (positions are already force-closed by then)."""
