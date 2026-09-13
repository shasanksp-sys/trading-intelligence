"""
"One Number Magic" -- same rule as strategies/one_number_magic.py, but
using the REAL mechanic the spec describes (sell a put/call near the
reference strike) instead of the earlier directional-proxy position.
Compare this result against one_number_magic.py's for the same period:
that comparison is the actual point -- it's what tells us whether
premium decay rescues a mediocre directional signal, exactly as the
spec's own stated logic claims ("even if it hits the stop loss, the time
erosion would have eaten the premium already").

Same two honest gaps as before: this strategy's own open_questions
aren't resolved by testing it, and this is exploratory, not an approval.
Additional approximations specific to this version: monthly expiry via
options_calendar.py's simplified calendar (not a real holiday-aware NSE
calendar), volatility from realized (not implied) history, and lot_size=1
(nominal, not NIFTY's real contract lot size) since this tests the
mechanic, not real capital sizing.
"""

from __future__ import annotations

from datetime import datetime

import pandas as pd

from ..broker import Broker
from ..options_calendar import last_thursday_of_month, round_to_strike
from ..strategy import Strategy
from ..types import OptionType
from ..volatility import realized_volatility


class OneNumberMagicOptionsStrategy(Strategy):
    def __init__(self, symbols: list[str], vol_window: int = 20, **params):
        super().__init__(symbols, **params)
        self.vol_window = vol_window
        self._period_ref: dict[str, float] = {}
        self._current_period: dict[str, tuple] = {}
        self._open_position: dict[str, str] = {}  # symbol -> option position_id

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            period_key = (timestamp.year, timestamp.month)
            is_new_period = self._current_period.get(symbol) != period_key
            open_pos_id = self._open_position.get(symbol)

            if is_new_period:
                # open_pos_id may already be stale here: the broker auto-settles
                # a position at its own expiry (engine.py calls
                # process_option_expiries every bar) without notifying this
                # strategy, so a tracked id can point at an already-removed
                # position. Check before closing, same defensive pattern as
                # the existing-position branch below -- caught by an actual
                # KeyError when this wasn't checked here.
                if open_pos_id is not None and broker.get_option_position(open_pos_id) is not None:
                    broker.close_option_position(open_pos_id, df["close"].iloc[-1], tag="period_end")
                self._open_position[symbol] = None
                self._current_period[symbol] = period_key
                self._period_ref[symbol] = df["open"].iloc[-1]
                continue

            ref = self._period_ref.get(symbol)
            if ref is None or len(df) < self.vol_window + 1:
                continue
            close = df["close"].iloc[-1]
            vol_series = realized_volatility(df["close"], window=self.vol_window)
            vol = vol_series.iloc[-1]
            if pd.isna(vol) or vol <= 0:
                continue

            open_pos_id = self._open_position.get(symbol)
            if open_pos_id is None:
                strike = round_to_strike(ref, symbol)
                expiry = datetime.combine(last_thursday_of_month(timestamp.year, timestamp.month), datetime.min.time())
                if expiry <= timestamp:
                    # this month's expiry already passed (bias flipped late in
                    # the month) -- roll to next month's contract, same as a
                    # real trader would once the current month's series expires
                    year, month = (timestamp.year + 1, 1) if timestamp.month == 12 else (timestamp.year, timestamp.month + 1)
                    expiry = datetime.combine(last_thursday_of_month(year, month), datetime.min.time())
                if close > ref:
                    # bullish bias -> sell the put near the reference strike
                    pos_id = broker.write_option(symbol, strike, expiry, OptionType.PUT,
                                                  lots=1, spot=close, volatility=vol)
                    self._open_position[symbol] = pos_id
                elif close < ref:
                    pos_id = broker.write_option(symbol, strike, expiry, OptionType.CALL,
                                                  lots=1, spot=close, volatility=vol)
                    self._open_position[symbol] = pos_id
            else:
                position = broker.get_option_position(open_pos_id)
                if position is None:
                    self._open_position[symbol] = None
                    continue
                bias_was_bullish = position.option_type == OptionType.PUT
                stopped_out = (bias_was_bullish and close < ref) or (not bias_was_bullish and close > ref)
                if stopped_out:
                    broker.close_option_position(open_pos_id, close, tag="bias_stopped_out")
                    self._open_position[symbol] = None
