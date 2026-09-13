"""
Ichimoku -- SIMPLIFIED VERSION only, as explicitly offered by the spec
itself (course_id 'Compiled FINAL - Main + Retake'): "use the baseline
alone as a trend filter -- above the baseline buy, below the baseline
sell". The baseline (Kijun-sen) is a standard, public-domain Ichimoku
Kinko Hyo component: (26-period high + 26-period low) / 2.

This is a deliberate, honest scope cut, not the full system described in
the spec -- NOT implemented here: the cloud, lagging span, W/M-pattern +
26-candle confluence entries, C-Clamp consolidation pattern, cloud-size
sustainability filter, 70% candle-close validity filter, or the 9/17/26
time-cycle projection. Those remain untested. Resolved open questions
(see conversation/memory): retake's advanced additions treated as
layering on the main course's base rules rather than replacing them (not
exercised by this simplified cut anyway); lot-scaling excluded from this
first test.

Exit mirrors the entry (close below baseline), matching the "above the
baseline buy, below the baseline sell" framing directly -- not the master
exit (lagging span vs cloud) or scaled exit from the full system, since
neither cloud nor lagging span is computed here.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


def _baseline(df: pd.DataFrame, period: int = 26) -> pd.Series:
    return (df["high"].rolling(period).max() + df["low"].rolling(period).min()) / 2


class IchimokuBaselineStrategy(Strategy):
    def __init__(self, symbols: list[str], baseline_period: int = 26, **params):
        super().__init__(symbols, **params)
        self.baseline_period = baseline_period

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < self.baseline_period + 1:
                continue
            baseline = _baseline(df, self.baseline_period)
            if pd.isna(baseline.iloc[-1]):
                continue

            close = df["close"].iloc[-1]
            position = broker.get_position(symbol)

            if not position.is_open:
                if close > baseline.iloc[-1]:
                    broker.buy(symbol, quantity=1)
                elif close < baseline.iloc[-1]:
                    broker.sell(symbol, quantity=1)
            elif position.is_long and close < baseline.iloc[-1]:
                broker.close_position(symbol, tag="baseline_cross")
            elif position.is_short and close > baseline.iloc[-1]:
                broker.close_position(symbol, tag="baseline_cross")
