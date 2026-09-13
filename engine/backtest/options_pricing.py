"""
Black-Scholes option pricing -- the standard, public-domain academic
formula (Black & Scholes, 1973) for a European option's theoretical
price, used here because we have no real historical options-chain data
(bid/ask, actual traded premiums) for NIFTY/BANK NIFTY options. This is
a genuine, stated approximation: real option prices trade with a
volatility skew/smile this flat-volatility model doesn't capture, and
Indian index options are American-style (early exercise, though rarely
optimal for index options without dividends) rather than the European
style this formula assumes. Good enough to test a strategy's LOGIC
(does the entry/exit/decay mechanic make sense), not a substitute for
real options-chain data before trusting a number enough to trade on.

No historical implied volatility exists either -- volatility.py's
realized-volatility estimator (computed from the underlying's own price
history) stands in for it. Real implied volatility usually differs from
realized, sometimes substantially, especially around events -- another
layer of approximation stacked on top of the pricing model itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .types import OptionType


def _norm_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


@dataclass
class OptionGreeks:
    price: float
    delta: float
    theta: float  # per calendar day, not per year


def black_scholes(
    spot: float,
    strike: float,
    days_to_expiry: float,
    volatility: float,
    option_type: OptionType,
    risk_free_rate: float = 0.065,
) -> OptionGreeks:
    """
    volatility: annualized, as a decimal (e.g. 0.15 for 15%) -- see
    volatility.py for estimating this from historical bars.
    days_to_expiry: calendar days remaining (fractional is fine); at or
    below 0, returns pure intrinsic value with zero theta (option has expired).
    """
    if days_to_expiry <= 0:
        intrinsic = max(spot - strike, 0.0) if option_type == OptionType.CALL else max(strike - spot, 0.0)
        return OptionGreeks(price=intrinsic, delta=1.0 if intrinsic > 0 else 0.0, theta=0.0)

    t = days_to_expiry / 365.0
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (risk_free_rate + 0.5 * volatility ** 2) * t) / (volatility * sqrt_t)
    d2 = d1 - volatility * sqrt_t

    if option_type == OptionType.CALL:
        price = spot * _norm_cdf(d1) - strike * math.exp(-risk_free_rate * t) * _norm_cdf(d2)
        delta = _norm_cdf(d1)
        theta_annual = (
            -(spot * _norm_pdf(d1) * volatility) / (2 * sqrt_t)
            - risk_free_rate * strike * math.exp(-risk_free_rate * t) * _norm_cdf(d2)
        )
    else:
        price = strike * math.exp(-risk_free_rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)
        delta = _norm_cdf(d1) - 1
        theta_annual = (
            -(spot * _norm_pdf(d1) * volatility) / (2 * sqrt_t)
            + risk_free_rate * strike * math.exp(-risk_free_rate * t) * _norm_cdf(-d2)
        )

    return OptionGreeks(price=max(price, 0.0), delta=delta, theta=theta_annual / 365.0)
