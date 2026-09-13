"""
First real course-extracted strategy tested end to end -- "One Number
Magic (Opening Price of Period)", course_id 'Compiled FINAL - Main +
Retake', strategy_id from db.strategies. Chosen because entry/exit/
stop_loss/target are ALL fully specified (0 insufficient_fields) with
real worked examples cited, unlike the other 28 which are still missing
pieces.

Rule, straight from the spec: take the period's opening price as a single
reference level. Bias is bullish while price stays above it, bearish
while below; a CONFIRMED CLOSE back through the level (not an intrabar
touch) both stops out and flips the bias. Monthly granularity chosen
here (one of the spec's five offered options -- weekly/monthly/quarterly/
half-yearly/yearly) since that's what the cited worked examples used.

TWO HONEST GAPS, not resolved by this file, deliberately not glossed
over:

1. The spec's actual instrument is OPTION WRITING (sell a put near the
   reference strike when bullish, sell a call when bearish, profiting
   from time decay while the bias holds) -- not a plain directional
   position. This engine has no options/premium/decay modeling yet, so
   this file tests the DIRECTIONAL BIAS RULE ONLY via a synthetic long/
   short equity-style position as a stand-in. A correct call on
   direction is necessary but NOT sufficient for the real strategy's
   profitability -- premium decay, strike selection, and the writer's
   capped-upside/open-downside risk profile are real economics this
   result says nothing about.
2. This strategy's own open_questions (relationship to the course's WDC/
   Anniversary Date strategies; an acknowledged consolidation failure
   mode) are NOT resolved by testing it -- this is an exploratory run on
   an otherwise well-specified rule, not a claim that Phase A is
   complete or that this strategy is "approved" for anything.

The stop is deliberately NOT implemented as a broker-level STOP order:
the spec requires a CONFIRMED CLOSE through the level, not an intrabar
touch, which is a different trigger condition than what set_exit_orders/
attached_stop check (bar high/low range). Using the wrong one here would
silently misrepresent the actual rule -- so this checks the close
directly in on_bar() instead.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..strategy import Strategy


class OneNumberMagicStrategy(Strategy):
    def __init__(self, symbols: list[str], **params):
        super().__init__(symbols, **params)
        self._period_ref: dict[str, float] = {}
        self._current_period: dict[str, tuple] = {}

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            period_key = (timestamp.year, timestamp.month)
            is_new_period = self._current_period.get(symbol) != period_key
            position = broker.get_position(symbol)

            if is_new_period:
                if position.is_open:
                    broker.close_position(symbol, tag="period_end")
                self._current_period[symbol] = period_key
                self._period_ref[symbol] = df["open"].iloc[-1]
                continue  # reference just set from TODAY's open -- direction judged from the next close onward

            ref = self._period_ref.get(symbol)
            if ref is None:
                continue
            close = df["close"].iloc[-1]

            if not position.is_open:
                if close > ref:
                    broker.buy(symbol, quantity=1)
                elif close < ref:
                    broker.sell(symbol, quantity=1)
            elif position.is_long and close < ref:
                broker.close_position(symbol, tag="bias_stopped_out")
            elif position.is_short and close > ref:
                broker.close_position(symbol, tag="bias_stopped_out")
