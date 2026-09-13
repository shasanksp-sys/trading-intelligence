"""
Realized (historical) volatility, annualized -- the stand-in for implied
volatility used by options_pricing.py, since no historical options-chain
IV data exists for this project yet (see that file's docstring for what
this approximation costs).
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def realized_volatility(close: pd.Series, window: int = 20, trading_days_per_year: int = 252) -> pd.Series:
    """
    Rolling annualized volatility from daily close-to-close log returns.
    Returns NaN for the first `window` bars (not enough history yet) --
    callers should skip trading until this is populated, same as any
    other rolling-indicator warm-up period.
    """
    log_returns = np.log(close / close.shift(1))
    return log_returns.rolling(window).std() * np.sqrt(trading_days_per_year)
