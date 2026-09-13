"""
Researched candidate, NOT course-sourced -- same caveat as
donchian_breakout.py. RSI-based mean-reversion (buy oversold, sell
overbought, exit on reversion to neutral) is standard technical-analysis
material, not from any specific instructor here. Untested claim until
backtested.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    return 100 - (100 / (1 + rs))


class RsiMeanReversionStrategy(Strategy):
    def __init__(self, symbols: list[str], rsi_period: int = 14,
                 oversold: float = 30, overbought: float = 70,
                 stop_pct: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.rsi_period = rsi_period
        self.oversold = oversold
        self.overbought = overbought
        self.stop_pct = stop_pct

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            if len(df) < self.rsi_period + 1:
                continue
            rsi = _rsi(df["close"], self.rsi_period)
            latest_rsi = rsi.iloc[-1]
            if pd.isna(latest_rsi):
                continue

            position = broker.get_position(symbol)
            close = df["close"].iloc[-1]

            if not position.is_open:
                if latest_rsi < self.oversold:
                    broker.buy(symbol, quantity=1, stop_price=close * (1 - self.stop_pct / 100))
                elif latest_rsi > self.overbought:
                    broker.sell(symbol, quantity=1, stop_price=close * (1 + self.stop_pct / 100))
            elif position.is_long and latest_rsi >= 50:
                broker.close_position(symbol, tag="rsi_reverted")
            elif position.is_short and latest_rsi <= 50:
                broker.close_position(symbol, tag="rsi_reverted")
