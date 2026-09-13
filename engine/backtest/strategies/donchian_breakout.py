"""
Researched candidate, NOT course-sourced -- no citation to a specific
instructor/timestamp exists for this one, unlike the course-extracted
strategies. Source is general trading literature: N-day channel breakout
is the widely-documented basis of classic systematic trend-following
(the "buy the highest high in N days" family of systems). Untested claim
until backtested -- same rule as everything else in this project.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


class DonchianBreakoutStrategy(Strategy):
    def __init__(self, symbols: list[str], channel_days: int = 20,
                 stop_pct: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.channel_days = channel_days
        self.stop_pct = stop_pct

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < self.channel_days + 1:
                continue
            position = broker.get_position(symbol)
            if position.is_open:
                continue

            # channel computed EXCLUDING today's bar, so today's close can be compared against it
            prior = df.iloc[-self.channel_days - 1:-1]
            channel_high, channel_low = prior["high"].max(), prior["low"].min()
            close = df["close"].iloc[-1]

            if close > channel_high:
                broker.buy(symbol, quantity=1, stop_price=close * (1 - self.stop_pct / 100))
            elif close < channel_low:
                broker.sell(symbol, quantity=1, stop_price=close * (1 + self.stop_pct / 100))
