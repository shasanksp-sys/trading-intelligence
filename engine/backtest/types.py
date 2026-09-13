"""
Shared data types for the backtest engine.

Kept deliberately broker/data-source agnostic -- these are the only
objects that flow between a Strategy, a Broker, and a DataFeed. Anything
strategy-specific (which symbols, what indicators, entry logic) lives in
the Strategy subclass, never here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class OrderSide(Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderStatus(Enum):
    PENDING = "PENDING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"


class OptionType(Enum):
    CALL = "CALL"
    PUT = "PUT"


@dataclass
class Order:
    symbol: str
    side: OrderSide
    quantity: float
    order_type: OrderType = OrderType.MARKET
    limit_price: float | None = None
    stop_price: float | None = None
    tag: str = ""                      # e.g. "entry", "stop_loss", "target" -- for reading trade logs
    oco_group: str | None = None       # orders sharing a group id: one filling cancels the rest
    attached_stop: float | None = None      # bracket: create a stop-loss once THIS order fills
    attached_target: float | None = None    # bracket: create a target once THIS order fills
    status: OrderStatus = OrderStatus.PENDING
    order_id: str = ""
    created_at: datetime | None = None


@dataclass
class Fill:
    order: Order
    fill_price: float
    fill_time: datetime
    commission: float = 0.0


@dataclass
class Position:
    symbol: str
    quantity: float = 0.0          # positive = long, negative = short, 0 = flat
    avg_price: float = 0.0

    @property
    def is_open(self) -> bool:
        return self.quantity != 0

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    @property
    def is_short(self) -> bool:
        return self.quantity < 0


@dataclass
class Trade:
    """One completed round-trip (entry -> exit), the unit backtest metrics are computed from."""
    symbol: str
    side: OrderSide            # side of the ENTRY (BUY = went long, SELL = went short)
    entry_time: datetime
    entry_price: float
    exit_time: datetime
    exit_price: float
    quantity: float
    commission: float = 0.0
    exit_reason: str = ""      # "stop_loss", "target", "signal", "end_of_backtest"

    @property
    def pnl(self) -> float:
        direction = 1 if self.side == OrderSide.BUY else -1
        return direction * (self.exit_price - self.entry_price) * self.quantity - self.commission

    @property
    def return_pct(self) -> float:
        if self.entry_price == 0:
            return 0.0
        direction = 1 if self.side == OrderSide.BUY else -1
        return direction * (self.exit_price - self.entry_price) / self.entry_price


@dataclass
class OptionPosition:
    """
    Deliberately separate from Position/Order -- an option's price comes
    from options_pricing.black_scholes() computed on demand, not from a
    data-feed bar, so it doesn't fit the bar-fill-queue machinery
    Order/_execute_fill are built around. See broker.py's options section
    docstring for the full reasoning.

    volatility is fixed at the entry premium's calculation and reused for
    every later mark-to-market/exit repricing of THIS position -- a
    stated simplification (real options have vega risk from volatility
    itself changing) documented in options_pricing.py.
    """
    position_id: str
    underlying: str
    strike: float
    expiry: datetime
    option_type: OptionType
    side: OrderSide            # SELL = written/short (collect premium), BUY = long (pay premium)
    lots: float
    lot_size: float
    entry_premium: float       # price per unit, before multiplying by lots * lot_size
    entry_time: datetime
    volatility: float


@dataclass
class OptionTrade:
    """One closed option position -- the options-world equivalent of Trade."""
    underlying: str
    strike: float
    expiry: datetime
    option_type: OptionType
    side: OrderSide
    lots: float
    lot_size: float
    entry_time: datetime
    entry_premium: float
    exit_time: datetime
    exit_premium: float
    commission: float = 0.0
    exit_reason: str = ""

    @property
    def pnl(self) -> float:
        per_unit = (self.entry_premium - self.exit_premium) if self.side == OrderSide.SELL \
            else (self.exit_premium - self.entry_premium)
        return per_unit * self.lots * self.lot_size - self.commission


@dataclass
class Bar:
    symbol: str
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class CommissionModel:
    """Applied per fill. Override either field to match your actual broker's fee schedule."""
    per_trade: float = 0.0
    percent_of_notional: float = 0.0

    def compute(self, quantity: float, price: float) -> float:
        return self.per_trade + abs(quantity * price) * self.percent_of_notional


@dataclass
class SlippageModel:
    """Applied unfavorably to every fill: buys fill higher, sells fill lower."""
    percent: float = 0.0

    def apply(self, price: float, side: OrderSide) -> float:
        direction = 1 if side == OrderSide.BUY else -1
        return price * (1 + direction * self.percent)
