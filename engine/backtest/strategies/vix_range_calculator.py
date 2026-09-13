"""
VIX-Based Range Calculator -- course_id 'Compiled FINAL - Main + Retake'.
Unlike POB/4-Number Convergence, this one's formula is fully given, not
withheld: expected range = (VIX / sqrt(days_in_period)) as a percent,
applied to the period's reference closing price -- the standard
"implied-move" calculation from options theory, not proprietary to this
course. Verified against the spec's own worked example (~0.76% daily
factor from a ~14.5 VIX reading) before trusting it.

Scope: monthly periodicity only (days_in_period=30, per spec), using the
main course's simpler "3 progressive same-size targets" rather than the
retake's 1x/1.5x/2x tiers, since the retake's aggressive-vs-normal-range
distinction is an open question the course material itself never
resolves (see conversation/memory) -- picking the fully-specified version
over the ambiguous one. Stop-loss is explicitly left open by the course
("use your own stop loss") -- this uses the reference closing price
itself (the origin point the whole range was projected from) as a
non-arbitrary invalidation level, rather than inventing an unrelated
number.

Needs TWO symbols together: the VIX index and the underlying
(NIFTY/BANKNIFTY). Requires both to have a bar on the same date to act --
a handful of dates exist where one has data and the other doesn't (see
the data-quality audit from earlier this session); those bars are
skipped rather than guessed at.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy

DAYS_IN_PERIOD = 30  # monthly, per spec
NUM_TARGET_TIERS = 3


class VixRangeCalculatorStrategy(Strategy):
    def __init__(self, symbols: list[str], vix_symbol: str = "INDIAVIX",
                 underlying_symbol: str = "NIFTY", **params):
        super().__init__(symbols, **params)
        self.vix_symbol = vix_symbol
        self.underlying_symbol = underlying_symbol
        self._current_month = None
        self._reference_close = None
        self._range_value = None
        self._traded_this_period = False

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        if self.vix_symbol not in history or self.underlying_symbol not in history:
            return
        vix_df, price_df = history[self.vix_symbol], history[self.underlying_symbol]
        symbol = self.underlying_symbol

        period_key = (timestamp.year, timestamp.month)
        if self._current_month != period_key:
            position = broker.get_position(symbol)
            if position.is_open:
                broker.close_position(symbol, tag="period_end")
            self._current_month = period_key
            self._reference_close = price_df["close"].iloc[-1]
            vix_value = vix_df["close"].iloc[-1]
            factor_pct = vix_value / (DAYS_IN_PERIOD ** 0.5)
            self._range_value = self._reference_close * factor_pct / 100
            self._traded_this_period = False
            return

        if self._reference_close is None or self._traded_this_period:
            return

        close = price_df["close"].iloc[-1]
        high_range = self._reference_close + self._range_value
        low_range = self._reference_close - self._range_value
        position = broker.get_position(symbol)

        if not position.is_open:
            if close > high_range:
                target = high_range + self._range_value * NUM_TARGET_TIERS
                broker.buy(symbol, quantity=1, target_price=target)
                self._traded_this_period = True
            elif close < low_range:
                target = low_range - self._range_value * NUM_TARGET_TIERS
                broker.sell(symbol, quantity=1, target_price=target)
                self._traded_this_period = True
        elif position.is_long and close < self._reference_close:
            broker.close_position(symbol, tag="stop_loss_close")
        elif position.is_short and close > self._reference_close:
            broker.close_position(symbol, tag="stop_loss_close")
