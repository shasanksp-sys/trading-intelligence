"""
Guest Price-Action System (Heikin-Ashi + Supertrend + Darvas Box) --
course_id 'Sameer Dharskar New 31-May Online Trading Course'. First
strategy from a SECOND, independently-processed course this session --
not a re-fit of the 29 already tested. Its one open question (relationship
to the course's money-management sizing) doesn't block testing the entry/
exit mechanic itself.

Scope cut, stated plainly: tests the core Heikin-Ashi + Supertrend
confluence on a SINGLE daily timeframe, not the spec's full multi-
timeframe (30min trend filter + 5min entry) pairing, and drops the
Darvas Box exit refinement -- both standard, well-documented additions
that could be layered on later, not implemented here to keep this a
single, verifiable first cut rather than three indicators' worth of
interacting logic at once.

Heikin-Ashi and Supertrend are both standard, public technical-analysis
formulas, not proprietary to this course:
  Heikin-Ashi: HA_close = (O+H+L+C)/4; HA_open = avg of prior bar's
  HA_open/HA_close (first bar: avg of that bar's own O/C). A run of
  same-colored HA candles signals trend strength; a color flip is exit.
  Supertrend: ATR-based trailing band (period=10, multiplier=3 here,
  the commonly-used defaults) that flips side when price crosses it.

Entry: HA candle is green AND close is above the Supertrend line -> long
(bullish confluence); mirror for short. Exit: HA color flips, or
Supertrend flips against the position -- whichever comes first, per the
spec's own stated exit rule.
"""

from __future__ import annotations

from datetime import datetime

import numpy as np
import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


def _heikin_ashi(df: pd.DataFrame) -> pd.DataFrame:
    ha_close = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha_open = pd.Series(index=df.index, dtype=float)
    ha_open.iloc[0] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2
    for i in range(1, len(df)):
        ha_open.iloc[i] = (ha_open.iloc[i - 1] + ha_close.iloc[i - 1]) / 2
    return pd.DataFrame({"ha_open": ha_open, "ha_close": ha_close})


def _supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0):
    """
    Standard Supertrend: ATR-based bands that only ever tighten toward
    price (never widen back out) until price closes through the band on
    the opposite side, which flips the trend direction. direction: 1 =
    bullish (supertrend line sits below price, acting as support), -1 =
    bearish (line sits above price, acting as resistance).
    """
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()

    hl2 = (high + low) / 2
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr

    final_upper = basic_upper.copy()
    final_lower = basic_lower.copy()
    direction = pd.Series(1, index=df.index, dtype=int)

    for i in range(1, len(df)):
        if pd.isna(atr.iloc[i]):
            continue
        if pd.isna(final_upper.iloc[i - 1]):
            # First valid bar right after ATR's warm-up: any comparison
            # against the still-NaN previous band evaluates to False in
            # numpy/Python, which would otherwise propagate NaN forward
            # forever (every later comparison keeps hitting a NaN
            # "previous" value) -- seed directly from the basic bands
            # instead of comparing against nothing. Caught by the
            # Supertrend column coming back entirely NaN on a real test.
            final_upper.iloc[i] = basic_upper.iloc[i]
            final_lower.iloc[i] = basic_lower.iloc[i]
        else:
            final_upper.iloc[i] = (basic_upper.iloc[i] if basic_upper.iloc[i] < final_upper.iloc[i - 1]
                                    or close.iloc[i - 1] > final_upper.iloc[i - 1] else final_upper.iloc[i - 1])
            final_lower.iloc[i] = (basic_lower.iloc[i] if basic_lower.iloc[i] > final_lower.iloc[i - 1]
                                    or close.iloc[i - 1] < final_lower.iloc[i - 1] else final_lower.iloc[i - 1])

        if direction.iloc[i - 1] == 1:
            direction.iloc[i] = -1 if close.iloc[i] < final_lower.iloc[i] else 1
        else:
            direction.iloc[i] = 1 if close.iloc[i] > final_upper.iloc[i] else -1

    supertrend = final_lower.where(direction == 1, final_upper)
    supertrend[atr.isna()] = float("nan")
    return supertrend, direction


class HeikinAshiSupertrendStrategy(Strategy):
    def __init__(self, symbols: list[str], st_period: int = 10, st_multiplier: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.st_period = st_period
        self.st_multiplier = st_multiplier

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < self.st_period + 5:
                continue
            ha = _heikin_ashi(df)
            supertrend, direction = _supertrend(df, self.st_period, self.st_multiplier)
            if pd.isna(supertrend.iloc[-1]):
                continue

            ha_green = ha["ha_close"].iloc[-1] > ha["ha_open"].iloc[-1]
            close = df["close"].iloc[-1]
            st_bullish = direction.iloc[-1] == 1
            position = broker.get_position(symbol)

            if not position.is_open:
                if ha_green and st_bullish:
                    broker.buy(symbol, quantity=1)
                elif not ha_green and not st_bullish:
                    broker.sell(symbol, quantity=1)
            elif position.is_long and (not ha_green or not st_bullish):
                broker.close_position(symbol, tag="signal_flip")
            elif position.is_short and (ha_green or st_bullish):
                broker.close_position(symbol, tag="signal_flip")
