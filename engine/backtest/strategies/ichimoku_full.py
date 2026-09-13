"""
Ichimoku -- full CORE system (main-course version): cloud position +
baseline + lagging-span confirmation together, not the baseline-only cut
tested earlier (ichimoku_baseline.py), which showed no edge. The
hypothesis being tested here directly: does requiring all three pillars
to agree recover the edge that stripping down to the baseline alone lost?

Standard, public-domain Ichimoku Kinko Hyo formulas (published 1960s/
displayed publicly since the 1980s) -- not from this course's own
wording, general technical-analysis material:
  Tenkan-sen (conversion, 9):  (9-period high + 9-period low) / 2
  Kijun-sen  (baseline, 26):   (26-period high + 26-period low) / 2
  Senkou A:  (Tenkan + Kijun) / 2, plotted 26 periods AHEAD
  Senkou B:  (52-period high + 52-period low) / 2, plotted 26 periods AHEAD
  Cloud: the band between Senkou A and Senkou B
  Chikou (lagging span): today's close, plotted 26 periods BACK

Causality note, since the cloud is nominally "shifted forward": the cloud
level relevant to TODAY was computed from Tenkan/Kijun/52-high-low data
AS OF 26 PERIODS AGO (.shift(26) on the already-computed span series) --
using only past data, no look-ahead. The lagging-span condition is
likewise evaluated causally as close[today] vs close[26 bars ago], the
real-time-tradeable form of "will today's close, once plotted back 26
bars, sit above the price action that was there" -- not compared against
a close 26 bars in the FUTURE, which isn't knowable yet.

Retake-only additions (C-Clamp, W/M-pattern + 26-candle confluence,
cloud-size sustainability filter, 9/17/26 time-cycle projection) remain
out of scope, per the earlier resolved decision to test core mechanics
before the more elaborate pattern-detection additions.

Entry: close above the cloud, above baseline, AND lagging-span condition
all agree (three-pillar confluence). Exit: close below the cloud (the
spec's own stated MASTER EXIT, overriding all other signals) -- baseline-
only scaled exits and the W/M-pattern exit are not implemented here.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


def _ichimoku(df: pd.DataFrame, tenkan_period=9, kijun_period=26, senkou_b_period=52, shift=26):
    tenkan = (df["high"].rolling(tenkan_period).max() + df["low"].rolling(tenkan_period).min()) / 2
    kijun = (df["high"].rolling(kijun_period).max() + df["low"].rolling(kijun_period).min()) / 2
    span_a = (tenkan + kijun) / 2
    span_b = (df["high"].rolling(senkou_b_period).max() + df["low"].rolling(senkou_b_period).min()) / 2

    cloud_top = pd.concat([span_a, span_b], axis=1).max(axis=1).shift(shift)
    cloud_bottom = pd.concat([span_a, span_b], axis=1).min(axis=1).shift(shift)
    lagging_bullish = df["close"] > df["close"].shift(shift)

    return kijun, cloud_top, cloud_bottom, lagging_bullish


class IchimokuFullStrategy(Strategy):
    def __init__(self, symbols: list[str], shift: int = 26, **params):
        super().__init__(symbols, **params)
        self.shift = shift
        self._warmup = 52 + shift + 1  # 52-period span_b needs the longest lookback, plus the forward shift

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < self._warmup:
                continue
            baseline, cloud_top, cloud_bottom, lagging_bullish = _ichimoku(df, shift=self.shift)
            if pd.isna(cloud_top.iloc[-1]) or pd.isna(cloud_bottom.iloc[-1]) or pd.isna(baseline.iloc[-1]):
                continue

            close = df["close"].iloc[-1]
            position = broker.get_position(symbol)

            if not position.is_open:
                bullish_confluence = (close > cloud_top.iloc[-1] and close > baseline.iloc[-1]
                                       and lagging_bullish.iloc[-1])
                bearish_confluence = (close < cloud_bottom.iloc[-1] and close < baseline.iloc[-1]
                                       and not lagging_bullish.iloc[-1])
                if bullish_confluence:
                    broker.buy(symbol, quantity=1)
                elif bearish_confluence:
                    broker.sell(symbol, quantity=1)
            elif position.is_long and close < cloud_bottom.iloc[-1]:
                broker.close_position(symbol, tag="cloud_break_exit")
            elif position.is_short and close > cloud_top.iloc[-1]:
                broker.close_position(symbol, tag="cloud_break_exit")
