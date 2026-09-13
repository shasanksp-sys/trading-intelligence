"""
Time Cycle Bias Strategy -- course_id 'Compiled FINAL - Main + Retake'.
The first strategy this session actually testable now that real intraday
data exists (Kaggle NIFTY/BANK NIFTY minute data) -- everything else this
shape was blocked on exactly this.

Uses only 5-minute bars, deriving BOTH readings from that single stream
rather than needing a second 15-minute symbol from the engine: the first
15-minute candle's high/low is mathematically identical whether read
natively or built from the first three 5-minute bars of the day, so this
avoids the added complexity of juggling two timeframe-symbols against the
engine's per-timestamp bar exposure.

Rule, straight from the spec: classify the day's first 5-min candle --
Open=High (bearish bias), Open=Low (bullish bias), or neither (no bias
filter). Mark the first 15 minutes' high/low. On a bearish-bias day: wait
for price to break the 15-min low, then retrace back to it -> SHORT,
stop at the reference candle's high. Mirror for bullish. On a "neither"
day: trade whichever side breaks with retracement, no directional
filter. The bias-FAILURE reversal case (a separate, second trade
opportunity per the spec) is explicitly out of scope here, same earlier
scoping decision as before -- this tests only the primary bias-
confirmation entry. Exit is the course-wide fixed 3:15 PM square-off,
handled by BacktestEngine's intraday_square_off_time rather than
strategy-side logic. At most ONE entry per day is taken, once the day's breakout+retracement
fires -- matches the spec's evident intent (a single bias-confirmation
trade) and fixes a real bug an earlier version of this file had: without
that gate, the reference high/low (fixed at 9:15am) go stale as the day
progresses, and once a position closes the same breakout condition can
immediately re-trigger using that now-irrelevant early-morning level,
producing dozens of nonsensical "stop_loss" exits that were actually
random drift back to a stale reference -- caught by reading the actual
trade log (many "stop_loss" rows showing a PROFIT, which is backwards)
rather than trusting an implausibly good headline Sharpe.

Target is the course-wide minimum 1:3 reward-to-
risk filter, same as WDC Date and Birthday Date.

"Open=High"/"Open=Low" are checked as exact equality (open literally
equals that bar's high/low) -- the spec describes this as a discrete,
visually-read chart classification, not an approximate one.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


class TimeCycleBiasStrategy(Strategy):
    def __init__(self, symbols: list[str], reward_risk_ratio: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.reward_risk_ratio = reward_risk_ratio
        self._current_day: dict[str, object] = {}
        self._day_bars: dict[str, list] = {}
        self._bias: dict[str, str | None] = {}       # "bullish" / "bearish" / "neither"
        self._ref_high: dict[str, float] = {}          # first 5-min candle's high (bearish-day stop)
        self._ref_low: dict[str, float] = {}            # first 5-min candle's low (bullish-day stop)
        self._range_high: dict[str, float] = {}         # first-15-min range
        self._range_low: dict[str, float] = {}
        self._breakout: dict[str, str | None] = {}      # None / "up" / "down", within the day
        self._traded_today: dict[str, bool] = {}        # at most one entry per day, see module docstring

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            bar = df.iloc[-1]
            day = timestamp.date()

            if self._current_day.get(symbol) != day:
                self._current_day[symbol] = day
                self._day_bars[symbol] = []
                self._bias[symbol] = None
                self._breakout[symbol] = None
                self._traded_today[symbol] = False

            self._day_bars[symbol].append(bar)
            bars_today = self._day_bars[symbol]
            n = len(bars_today)

            if n == 1:
                first = bars_today[0]
                if first["open"] == first["high"]:
                    self._bias[symbol] = "bearish"
                elif first["open"] == first["low"]:
                    self._bias[symbol] = "bullish"
                else:
                    self._bias[symbol] = "neither"
                self._ref_high[symbol] = first["high"]
                self._ref_low[symbol] = first["low"]
                continue

            if n == 3:
                self._range_high[symbol] = max(b["high"] for b in bars_today[:3])
                self._range_low[symbol] = min(b["low"] for b in bars_today[:3])
                continue

            if n < 4:
                continue

            position = broker.get_position(symbol)
            if position.is_open or self._traded_today.get(symbol):
                continue  # at most one entry per day (see module docstring); exit only via stop/target/3:15 square-off

            range_high, range_low = self._range_high.get(symbol), self._range_low.get(symbol)
            bias = self._bias.get(symbol)
            breakout = self._breakout.get(symbol)

            allow_short = bias in ("bearish", "neither")
            allow_long = bias in ("bullish", "neither")

            if breakout is None:
                if allow_short and bar["low"] < range_low:
                    self._breakout[symbol] = "down"
                elif allow_long and bar["high"] > range_high:
                    self._breakout[symbol] = "up"
            elif breakout == "down" and bar["high"] >= range_low:
                stop = self._ref_high[symbol]
                risk = stop - range_low
                broker.sell(symbol, quantity=1, stop_price=stop,
                            target_price=range_low - risk * self.reward_risk_ratio)
                self._traded_today[symbol] = True
            elif breakout == "up" and bar["low"] <= range_high:
                stop = self._ref_low[symbol]
                risk = range_high - stop
                self._traded_today[symbol] = True
                broker.buy(symbol, quantity=1, stop_price=stop,
                           target_price=range_high + risk * self.reward_risk_ratio)
