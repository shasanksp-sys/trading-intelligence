"""
Simplified expiry-date and strike-rounding helpers for NSE index options.

APPROXIMATION, stated plainly: real NSE monthly expiry is the last
Thursday of the month (shifted to the prior trading day on an exchange
holiday) -- this doesn't account for holidays, only weekends. Good enough
for testing a strategy's decay/direction logic against Black-Scholes
pricing; not a substitute for a real NSE trading-calendar/holiday feed
before trusting an exact expiry-day number.
"""

from __future__ import annotations

from datetime import date, timedelta

STRIKE_INTERVAL = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "MIDCAPNIFTY": 25,
    "SENSEX": 100,
}


def last_thursday_of_month(year: int, month: int) -> date:
    if month == 12:
        next_month_first = date(year + 1, 1, 1)
    else:
        next_month_first = date(year, month + 1, 1)
    last_day = next_month_first - timedelta(days=1)
    offset = (last_day.weekday() - 3) % 7  # Thursday = weekday 3
    return last_day - timedelta(days=offset)


def round_to_strike(price: float, symbol: str) -> float:
    interval = STRIKE_INTERVAL.get(symbol.upper(), 50)
    return round(price / interval) * interval
