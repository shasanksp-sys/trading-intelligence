"""
Birthday/Anniversary Date Strategy -- course_id 'Compiled FINAL - Main +
Retake'. Testing the BIRTHDAY variant only: the Anniversary variant needs
a stock's IPO listing date, which doesn't apply to indices (NIFTY/
BANKNIFTY have no listing date) and we have no per-stock listing-date
data anyway. The birthday date itself is inherently arbitrary per the
spec ("mark the candle on the trader's own birthday") -- this tests
whether the MECHANIC (one annual reference candle, breakout+retracement,
hold to next year) has any edge, independent of which specific calendar
day is used, so the date defaults to Jan 1 and is a parameter, not a
claim about any specific date being special.

If the exact date falls on a non-trading day, the spec says to use one
day before or after (explicitly interchangeable) -- approximated here as
the nearest trading day actually present in the data.

Mechanically identical to wdc_date.py (mark reference high/low, wait for
a confirmed close beyond it, wait for retracement back to enter, close-
based stop at the reference candle's opposite side, course-wide 1:3
minimum reward-to-risk target, hold until the next occurrence) --
deliberately reusing the same shape since the spec's own open questions
describe these as belonging to the same concept-family.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


class BirthdayDateStrategy(Strategy):
    def __init__(self, symbols: list[str], month: int = 1, day: int = 1,
                 reward_risk_ratio: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.month = month
        self.day = day
        self.reward_risk_ratio = reward_risk_ratio
        self._ref_high: dict[str, float] = {}
        self._ref_low: dict[str, float] = {}
        self._breakout: dict[str, str | None] = {}
        self._marked_year: dict[str, int] = {}  # avoid re-marking multiple times near the target date

    def _is_reference_day(self, timestamp: datetime, symbol: str, df: pd.DataFrame) -> bool:
        if self._marked_year.get(symbol) == timestamp.year:
            return False
        target = pd.Timestamp(year=timestamp.year, month=self.month, day=self.day)
        # nearest trading day actually in the data, approximating the
        # spec's "one day before or after if it falls on a holiday"
        window = df.index[(df.index >= target - pd.Timedelta(days=3)) & (df.index <= target + pd.Timedelta(days=3))]
        if len(window) == 0:
            return False
        nearest_idx = np.argmin(np.abs((window - target).values))
        return timestamp == window[nearest_idx]

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            bar = df.iloc[-1]
            position = broker.get_position(symbol)

            if self._is_reference_day(timestamp, symbol, df):
                if position.is_open:
                    broker.close_position(symbol, tag="new_reference_year")
                self._ref_high[symbol] = bar["high"]
                self._ref_low[symbol] = bar["low"]
                self._breakout[symbol] = None
                self._marked_year[symbol] = timestamp.year
                continue

            ref_high, ref_low = self._ref_high.get(symbol), self._ref_low.get(symbol)
            if ref_high is None:
                continue

            if position.is_open:
                if position.is_long and bar["close"] < ref_low:
                    broker.close_position(symbol, tag="stop_loss_close")
                elif position.is_short and bar["close"] > ref_high:
                    broker.close_position(symbol, tag="stop_loss_close")
                continue

            breakout = self._breakout.get(symbol)
            if breakout is None:
                if bar["close"] > ref_high:
                    self._breakout[symbol] = "up"
                elif bar["close"] < ref_low:
                    self._breakout[symbol] = "down"
            elif breakout == "up" and bar["low"] <= ref_high:
                risk = ref_high - ref_low
                broker.buy(symbol, quantity=1, target_price=bar["close"] + risk * self.reward_risk_ratio)
            elif breakout == "down" and bar["high"] >= ref_low:
                risk = ref_high - ref_low
                broker.sell(symbol, quantity=1, target_price=bar["close"] - risk * self.reward_risk_ratio)
