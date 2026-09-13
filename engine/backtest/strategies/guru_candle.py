"""
Guru Candle (144th-Candle / Jupiter Number System) -- course_id
'Compiled FINAL - Main + Retake'. Resolved open questions: no fixed
target -> course-wide 1:3 reward-to-risk filter applied (as with every
other strategy in this course lacking its own target formula); the
student's proposed re-entry refinement is NOT used since the spec only
confirms it was "acknowledged positively," not adopted as the standard
rule -- the base re-entry rule (allowed after a stop-out) is what's
implemented; relationship to God Strategy doesn't block testing this as
an independent strategy.

Rule: mark the 144th one-minute candle's high/low from the day's open.
Wait for a CLOSE beyond that level (breakout), then a wick-touch
retracement back to it (a touch counts, a close through isn't required
for the retest itself) -- enter in the breakout direction. Stop is a
CONFIRMED CLOSE beyond the reference level (not a touch) -- approximated
here as the 144th candle's own opposite extreme (low for a long, high for
a short), since the spec's "relevant recent swing low/high" wording
doesn't fully specify a programmatic swing-detection rule beyond the
reference candle itself. Target: course-wide minimum 1:3 R:R. Exit:
mandatory 3:15pm square-off (this course's fixed rule for every intraday
strategy).

RE-ENTRY, explicitly allowed by the spec after a stop-out (unlike
WDC Date/Time Cycle Bias/Momentum-Filtered Straddle, which are
deliberately gated to one entry per day -- this one is NOT, on the
spec's own explicit instruction): implemented as resetting the breakout
state after a stop-out so the SAME 144th-candle reference level can be
breached and retested again later the same day, rather than building a
full recent-swing-tracking algorithm the spec doesn't fully specify.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy

REFERENCE_CANDLE_NUMBER = 144


class GuruCandleStrategy(Strategy):
    def __init__(self, symbols: list[str], reward_risk_ratio: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.reward_risk_ratio = reward_risk_ratio
        self._current_day: dict[str, object] = {}
        self._day_bar_count: dict[str, int] = {}
        self._ref_high: dict[str, float] = {}
        self._ref_low: dict[str, float] = {}
        self._breakout: dict[str, str | None] = {}

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            bar = df.iloc[-1]
            day = timestamp.date()

            if self._current_day.get(symbol) != day:
                self._current_day[symbol] = day
                self._day_bar_count[symbol] = 0
                self._ref_high[symbol] = None
                self._ref_low[symbol] = None
                self._breakout[symbol] = None

            self._day_bar_count[symbol] += 1
            if self._day_bar_count[symbol] == REFERENCE_CANDLE_NUMBER:
                self._ref_high[symbol] = bar["high"]
                self._ref_low[symbol] = bar["low"]
                continue

            ref_high, ref_low = self._ref_high.get(symbol), self._ref_low.get(symbol)
            if ref_high is None:
                continue

            position = broker.get_position(symbol)
            if position.is_open:
                # close-based stop, per this course's repeated close-not-touch principle
                if position.is_long and bar["close"] < ref_low:
                    broker.close_position(symbol, tag="stop_loss_close")
                    self._breakout[symbol] = None  # re-entry allowed, per spec
                elif position.is_short and bar["close"] > ref_high:
                    broker.close_position(symbol, tag="stop_loss_close")
                    self._breakout[symbol] = None
                continue

            breakout = self._breakout[symbol]
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
