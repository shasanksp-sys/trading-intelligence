"""
Gap-fill: a candidate archetype (not a proven edge -- see spot_analysis.py
and the roadmap memory for why nothing here is "sure shot"), formalized
and backtested like any other candidate.

Rule: when the session opens with a gap beyond `gap_threshold_pct` versus
the prior close, bet on reversion back toward that prior close rather than
continuation. Long on a gap-down (buy the dip), short on a gap-up (fade
the spike), protected by a stop/target bracket. Exits only via that
bracket (or the engine's end-of-backtest force-close if neither triggers)
-- there is deliberately no separate "close after N bars" signal here: an
earlier draft tried to force-close one bar after entry as a "same-day"
approximation, but broker.close_position() cancels resting orders before
submitting its own close, which would have cancelled the stop/target
bracket before it ever got a chance to trigger. Caught by tracing the
actual fill-timing model rather than trusting the first draft -- exactly
the kind of bug the project's own "verify before trusting" principle
exists to catch.

This is a daily-bar approximation of an intraday idea, since that's what
our current data depth actually supports; a real intraday version needs
real intraday data (Arrow/Upstox/Kaggle, still pending).
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


class GapFillStrategy(Strategy):
    def __init__(self, symbols: list[str], gap_threshold_pct: float = 0.3,
                 stop_pct: float = 0.5, target_pct: float = 0.3, **params):
        super().__init__(symbols, **params)
        self.gap_threshold_pct = gap_threshold_pct
        self.stop_pct = stop_pct
        self.target_pct = target_pct

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < 2:
                continue
            position = broker.get_position(symbol)
            if position.is_open:
                continue  # exits only via the stop/target bracket -- see module docstring

            prev_close = df["close"].iloc[-2]
            today_open = df["open"].iloc[-1]
            gap_pct = (today_open - prev_close) / prev_close * 100

            if gap_pct > self.gap_threshold_pct:
                # gapped up -- fade it, betting on reversion toward prev_close
                broker.sell(
                    symbol, quantity=1,
                    stop_price=today_open * (1 + self.stop_pct / 100),
                    target_price=today_open * (1 - self.target_pct / 100),
                )
            elif gap_pct < -self.gap_threshold_pct:
                # gapped down -- buy it, betting on reversion toward prev_close
                broker.buy(
                    symbol, quantity=1,
                    stop_price=today_open * (1 - self.stop_pct / 100),
                    target_price=today_open * (1 + self.target_pct / 100),
                )
