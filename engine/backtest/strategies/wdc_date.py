"""
WDC Date Strategy -- course_id 'Compiled FINAL - Main + Retake'. Resolved
open questions (see conversation/memory, not re-litigated here): the
"WDC" acronym mismatch is cosmetic, doesn't affect the actual rule; no
strategy-specific target exists, so the course-wide minimum 1:3
reward-to-risk filter (stated in the spec's own 'target' field, sourced
from repeated orientation-session statements) is applied; the mentioned
equinox/solstice dates are explicitly out of scope per the spec itself.

Rule, straight from the spec: a WDC date is any calendar day where
day-of-month + month-number equals the year's last two digits, skipped
entirely (not shifted) if it falls on a non-trading day. Mark that day's
high/low as the reference. Wait for a CLOSE beyond the level, then wait
for a RETRACEMENT back to that level before entering -- approximated here
as: after a confirmed close beyond the level, enter on the first later
bar whose range touches back to it, at that day's close (this engine's
MARKET orders fill at the next bar's open regardless, so entry timing is
already conservative by one bar). Stop is the reference candle's
high/low; target is stop-distance x 3, minimum-R:R rather than a
strategy-specific formula. Reference stays valid until the next genuine
WDC date -- a new WDC date supersedes and closes any existing position,
same "hold until next occurrence" pattern already confirmed for the
Birthday/Anniversary Date Strategy.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


def _is_wdc_date(d) -> bool:
    return (d.day + d.month) % 100 == d.year % 100


class WdcDateStrategy(Strategy):
    def __init__(self, symbols: list[str], reward_risk_ratio: float = 3.0, **params):
        super().__init__(symbols, **params)
        self.reward_risk_ratio = reward_risk_ratio
        self._ref_high: dict[str, float] = {}
        self._ref_low: dict[str, float] = {}
        self._breakout: dict[str, str | None] = {}   # None / "up" / "down"

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            bar = df.iloc[-1]
            position = broker.get_position(symbol)

            if _is_wdc_date(timestamp):
                if position.is_open:
                    broker.close_position(symbol, tag="new_wdc_date")
                self._ref_high[symbol] = bar["high"]
                self._ref_low[symbol] = bar["low"]
                self._breakout[symbol] = None
                continue

            ref_high, ref_low = self._ref_high.get(symbol), self._ref_low.get(symbol)
            if ref_high is None:
                continue  # no WDC date has occurred yet in this backtest window

            if position.is_open:
                # close-based stop, consistent with this course's repeated
                # close-not-touch confirmation principle
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
                # target only in the bracket (touch-based target is fine/standard);
                # the stop is the manual close-based check above, NOT a resting
                # touch-triggered STOP order -- passing stop_price here too would
                # run two conflicting stop mechanisms against this course's own
                # stated close-not-touch confirmation principle.
                risk = ref_high - ref_low
                broker.buy(symbol, quantity=1, target_price=bar["close"] + risk * self.reward_risk_ratio)
            elif breakout == "down" and bar["high"] >= ref_low:
                risk = ref_high - ref_low
                broker.sell(symbol, quantity=1, target_price=bar["close"] - risk * self.reward_risk_ratio)
