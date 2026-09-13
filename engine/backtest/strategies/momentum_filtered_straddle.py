"""
Momentum-Filtered Straddle -- course_id 'Compiled FINAL - Main + Retake'.
Resolved open questions (see conversation/memory): different presenter/
illustrative framing noted but treated as a valid candidate; momentum
reference = each leg's OWN premium at straddle initiation (day's open);
if both legs show momentum simultaneously, keep both (equivalent to the
plain unfiltered straddle for that case).

Rule: at day start, buy neither leg -- just record each leg's reference
premium. Through the day, the first leg (call or put) whose premium
moves >=10 points from ITS OWN reference gets bought (a plain long
option, not written); apply a fixed 10-point stop and 20-point target
from that leg's own entry premium. Same-day only -- exit is the mandatory
3:15pm square-off if neither stop nor target fires first (engine.py's
intraday_square_off_time, extended this session to also close option
positions, not just equity ones).

Strike: ATM, rounded to the day's opening spot via options_calendar's
per-instrument interval. Expiry: nearest weekly Thursday >= today,
approximating NSE's weekly options cycle (see options_calendar.py's own
stated holiday-calendar caveat). Volatility: realized, computed from this
symbol's OWN 5-minute data resampled to daily closes -- deliberately not
wiring in a second daily-bar data source just for this, at the cost of
volatility being estimated from this instrument's intraday history
specifically rather than a cleaner daily series.

This engine's options subsystem (broker.py) has no built-in bracket/stop
mechanism for options the way buy()/sell() has for equities -- the 10pt
stop / 20pt target here are checked manually against the position's
current mark-to-market premium each bar, not via a resting order.
"""

from __future__ import annotations

from datetime import datetime, time as dtime_cls, timedelta

import pandas as pd

from ..broker import Broker
from ..options_calendar import round_to_strike
from ..options_pricing import black_scholes
from ..strategy import Strategy
from ..types import OptionType
from ..volatility import realized_volatility

UNDERLYING_NAME = {"NIFTY_5MIN": "NIFTY", "BANKNIFTY_5MIN": "BANKNIFTY"}


def _next_thursday(d) -> datetime:
    """
    End-of-trading-day on the nearest Thursday >= d, NOT midnight -- an
    option expiring THIS Thursday is still valid for trading until market
    close that same day. Using midnight made same-day (today-is-Thursday)
    entries fail with "expiry already in the past" as soon as any
    intraday bar later than 00:00 was reached, caught by an actual
    exception rather than a silently wrong result.
    """
    days_ahead = (3 - d.weekday()) % 7  # Thursday = weekday 3
    target = d + timedelta(days=days_ahead)
    return datetime.combine(target, dtime_cls(15, 30))


class MomentumFilteredStraddleStrategy(Strategy):
    def __init__(self, symbols: list[str], momentum_threshold: float = 10.0,
                 stop_points: float = 10.0, target_points: float = 20.0, **params):
        super().__init__(symbols, **params)
        self.momentum_threshold = momentum_threshold
        self.stop_points = stop_points
        self.target_points = target_points
        self._current_day: dict[str, object] = {}
        self._strike: dict[str, float] = {}
        self._expiry: dict[str, datetime] = {}
        self._vol: dict[str, float] = {}
        self._ref_premium: dict[str, dict[OptionType, float]] = {}
        self._position_id: dict[str, dict[OptionType, str | None]] = {}
        self._traded_today: dict[str, dict[OptionType, bool]] = {}

    def on_bar(self, timestamp: datetime, history: dict[str, pd.DataFrame], broker: Broker) -> None:
        for symbol, df in history.items():
            underlying = UNDERLYING_NAME.get(symbol.upper(), symbol)
            day = timestamp.date()
            bar = df.iloc[-1]
            spot = bar["close"]

            if self._current_day.get(symbol) != day:
                self._current_day[symbol] = day
                # square off should have already closed any open legs; guard anyway
                for opt_type, pos_id in self._position_id.get(symbol, {}).items():
                    if pos_id is not None and broker.get_option_position(pos_id) is not None:
                        broker.close_option_position(pos_id, spot, tag="new_day")

                daily_closes = df["close"].resample("1D").last().dropna()
                vol_series = realized_volatility(daily_closes, window=20)
                vol = vol_series.iloc[-1] if len(vol_series) else float("nan")
                if pd.isna(vol) or vol <= 0:
                    vol = 0.15  # fallback for early history with insufficient daily samples

                strike = round_to_strike(bar["open"], underlying)
                expiry = _next_thursday(day)
                days_to_expiry = max((expiry - datetime.combine(day, datetime.min.time())).days, 1)

                self._strike[symbol] = strike
                self._expiry[symbol] = expiry
                self._vol[symbol] = vol
                self._ref_premium[symbol] = {
                    OptionType.CALL: black_scholes(bar["open"], strike, days_to_expiry, vol, OptionType.CALL).price,
                    OptionType.PUT: black_scholes(bar["open"], strike, days_to_expiry, vol, OptionType.PUT).price,
                }
                self._position_id[symbol] = {OptionType.CALL: None, OptionType.PUT: None}
                self._traded_today[symbol] = {OptionType.CALL: False, OptionType.PUT: False}
                continue

            strike, expiry, vol = self._strike[symbol], self._expiry[symbol], self._vol[symbol]
            days_to_expiry = max((expiry - datetime.combine(day, datetime.min.time())).days, 0)
            positions = self._position_id[symbol]

            for opt_type in (OptionType.CALL, OptionType.PUT):
                current_premium = black_scholes(spot, strike, days_to_expiry, vol, opt_type).price
                pos_id = positions[opt_type]

                if pos_id is None:
                    if self._traded_today[symbol][opt_type]:
                        continue  # at most one entry per leg per day -- see module docstring
                    moved = current_premium - self._ref_premium[symbol][opt_type]
                    if moved >= self.momentum_threshold:
                        positions[opt_type] = broker.buy_option(
                            underlying, strike, expiry, opt_type, lots=1, spot=spot, volatility=vol)
                        self._traded_today[symbol][opt_type] = True
                else:
                    position = broker.get_option_position(pos_id)
                    if position is None:
                        positions[opt_type] = None
                        continue
                    change = current_premium - position.entry_premium
                    if change <= -self.stop_points:
                        broker.close_option_position(pos_id, spot, tag="stop_loss")
                        positions[opt_type] = None
                    elif change >= self.target_points:
                        broker.close_option_position(pos_id, spot, tag="target")
                        positions[opt_type] = None
