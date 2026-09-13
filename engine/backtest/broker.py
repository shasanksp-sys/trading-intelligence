"""
Order execution and portfolio bookkeeping.

Broker is the seam this whole engine is built around: a Strategy only
ever calls methods on a Broker (buy/sell/set_exit_orders/get_position/
get_cash) -- it never touches a data feed or knows whether it's running
in a backtest. SimulatedBroker below is the backtest-time implementation
(simulates fills against historical bars). A future LiveBroker would
implement the exact same interface against a real broker's API (Zerodha,
Alpaca, Interactive Brokers, whichever) -- at that point a strategy that
already passed backtesting runs live with a one-line swap in engine
wiring, not a rewrite. See engine.py for how the swap point is used.

Fill timing, to avoid look-ahead bias:
  - MARKET orders submitted while reacting to bar T's data fill at bar
    T+1's OPEN (you can't have traded on information before it existed).
  - STOP / LIMIT orders (used for stop-loss / target) rest until a future
    bar's high-low range crosses their trigger price, then fill AT the
    trigger price (not the bar's extreme) -- a deliberately conservative
    assumption, since intrabar order-of-execution isn't knowable from
    OHLC bars alone.
  - Because an entry doesn't fill in the same on_bar() call it's
    submitted in, a strategy can't call set_exit_orders() right after
    buy()/sell() -- there's no position yet. Pass stop_price/target_price
    directly to buy()/sell() instead (a bracket order); the stop/target
    get attached automatically the moment the entry actually fills.
    set_exit_orders() itself is for adjusting exits on a position that's
    already open (e.g. a trailing stop).

Scope, stated explicitly rather than silently assumed: at most one open
position per symbol at a time (no pyramiding / partial scaling in the
same symbol). Any number of symbols and any number of concurrent
strategies are fine; this restriction is only about stacking multiple
entries into the same symbol, and is an intentional v1 boundary, not a
strategy-shape limitation -- lift it here if a strategy actually needs it.
"""

from __future__ import annotations

import itertools
from abc import ABC, abstractmethod
from datetime import datetime

from .options_pricing import black_scholes
from .types import (
    Bar, CommissionModel, Fill, Order, OrderSide, OrderStatus, OrderType,
    OptionPosition, OptionTrade, OptionType, Position, SlippageModel, Trade,
)

_order_ids = itertools.count(1)


class Broker(ABC):
    @abstractmethod
    def submit_order(self, order: Order) -> Order: ...

    @abstractmethod
    def get_position(self, symbol: str) -> Position: ...

    @abstractmethod
    def get_cash(self) -> float: ...


class SimulatedBroker(Broker):
    def __init__(
        self,
        starting_cash: float,
        commission: CommissionModel | None = None,
        slippage: SlippageModel | None = None,
    ):
        self.cash = starting_cash
        self.commission = commission or CommissionModel()
        self.slippage = slippage or SlippageModel()

        self._positions: dict[str, Position] = {}
        self._entry_records: dict[str, dict] = {}   # open position's entry info, for Trade construction on close
        self._pending_market_orders: list[Order] = []  # fill at NEXT bar's open
        self._resting_orders: dict[str, Order] = {}     # STOP/LIMIT orders, keyed by order_id

        self.fills: list[Fill] = []
        self.trades: list[Trade] = []
        self.equity_curve: list[tuple[datetime, float]] = []

        # ---- Options (see the "Options" section below) --------------------
        self._option_positions: dict[str, OptionPosition] = {}
        self.option_trades: list[OptionTrade] = []
        self._current_time: datetime | None = None  # set on every process_bar() call

    # ---- Broker interface --------------------------------------------

    def submit_order(self, order: Order) -> Order:
        order.order_id = str(next(_order_ids))
        if order.order_type == OrderType.MARKET:
            self._pending_market_orders.append(order)
        else:
            self._resting_orders[order.order_id] = order
        return order

    def get_position(self, symbol: str) -> Position:
        return self._positions.get(symbol, Position(symbol=symbol))

    def get_cash(self) -> float:
        return self.cash

    def get_equity(self, current_prices: dict[str, float]) -> float:
        equity = self.cash
        for symbol, position in self._positions.items():
            if position.is_open and symbol in current_prices:
                equity += position.quantity * current_prices[symbol]
        equity += self._option_unrealized_value(current_prices)
        return equity

    # ---- Strategy-facing convenience methods --------------------------

    def buy(self, symbol: str, quantity: float, order_type: OrderType = OrderType.MARKET,
            limit_price: float | None = None, tag: str = "entry",
            stop_price: float | None = None, target_price: float | None = None) -> Order:
        """
        stop_price/target_price make this a bracket order: once the entry
        fills (next bar's open for a MARKET order -- it hasn't happened
        yet when this call returns), the stop-loss/target are attached
        automatically. Needed because an entry and its exits can't both
        be submitted against an already-open position in the same
        on_bar() call -- the position doesn't exist until the entry fills.
        """
        return self.submit_order(Order(
            symbol=symbol, side=OrderSide.BUY, quantity=quantity,
            order_type=order_type, limit_price=limit_price, tag=tag,
            attached_stop=stop_price, attached_target=target_price,
        ))

    def sell(self, symbol: str, quantity: float, order_type: OrderType = OrderType.MARKET,
             limit_price: float | None = None, tag: str = "entry",
             stop_price: float | None = None, target_price: float | None = None) -> Order:
        """See buy() -- same bracket-order behavior, for opening a short."""
        return self.submit_order(Order(
            symbol=symbol, side=OrderSide.SELL, quantity=quantity,
            order_type=order_type, limit_price=limit_price, tag=tag,
            attached_stop=stop_price, attached_target=target_price,
        ))

    def close_position(self, symbol: str, tag: str = "signal") -> Order | None:
        position = self.get_position(symbol)
        if not position.is_open:
            return None
        side = OrderSide.SELL if position.is_long else OrderSide.BUY
        self._cancel_resting_orders_for(symbol)
        return self.submit_order(Order(
            symbol=symbol, side=side, quantity=abs(position.quantity),
            order_type=OrderType.MARKET, tag=tag,
        ))

    def set_exit_orders(self, symbol: str, stop_price: float | None = None,
                         target_price: float | None = None) -> None:
        """
        Attaches a stop-loss and/or target to the CURRENT open position,
        as one-cancels-the-other resting orders -- whichever triggers
        first closes the position and cancels the other automatically.
        Call this right after opening a position (or any time to replace
        existing exit orders with new levels, e.g. a trailing stop).
        """
        position = self.get_position(symbol)
        if not position.is_open:
            raise ValueError(f"set_exit_orders({symbol}): no open position to attach exits to")
        self._cancel_resting_orders_for(symbol)
        self._attach_exit_orders(symbol, position.quantity, stop_price, target_price)

    def _attach_exit_orders(self, symbol: str, position_quantity: float,
                             stop_price: float | None, target_price: float | None) -> None:
        if stop_price is None and target_price is None:
            return
        exit_side = OrderSide.SELL if position_quantity > 0 else OrderSide.BUY
        group = f"{symbol}-oco-{next(_order_ids)}"
        # Submitted stop-first: if one bar's range crosses BOTH trigger
        # prices (can't happen from OHLC alone -- an all-time-high/low
        # bar that spans both), the stop is checked and filled first and
        # cancels the target, per the conservative assume-the-worst
        # tie-break documented at the top of this file.
        if stop_price is not None:
            self.submit_order(Order(
                symbol=symbol, side=exit_side, quantity=abs(position_quantity),
                order_type=OrderType.STOP, stop_price=stop_price,
                tag="stop_loss", oco_group=group,
            ))
        if target_price is not None:
            self.submit_order(Order(
                symbol=symbol, side=exit_side, quantity=abs(position_quantity),
                order_type=OrderType.LIMIT, limit_price=target_price,
                tag="target", oco_group=group,
            ))

    # ---- Engine-facing: called once per symbol at the start of each new bar

    def process_bar(self, bar: Bar) -> None:
        self._current_time = bar.timestamp
        self._fill_pending_market_orders(bar)
        self._check_resting_orders(bar)

    def force_close_all(self, prices: dict[str, float], timestamp: datetime, reason: str) -> None:
        """
        Called once at the end of the backtest so every open position
        lands in the trade log. Routed through _execute_fill (the same
        path every real exit takes) rather than touching cash separately
        here -- a prior version updated the Trade record directly and
        forgot to move cash, which silently understated final equity by
        exactly the closed position's notional value whenever a position
        was still open at the backtest's end.
        """
        for symbol, position in list(self._positions.items()):
            if position.is_open and symbol in prices:
                side = OrderSide.SELL if position.is_long else OrderSide.BUY
                order = Order(
                    symbol=symbol, side=side, quantity=abs(position.quantity),
                    order_type=OrderType.MARKET, tag=reason,
                )
                order.order_id = str(next(_order_ids))
                self._execute_fill(order, prices[symbol], timestamp)

    # ---- Options --------------------------------------------------------
    #
    # Deliberately a separate, parallel subsystem from buy/sell/Position
    # above, not an extension of it: an option's price comes from
    # options_pricing.black_scholes() computed on demand from
    # (spot, strike, days-to-expiry, volatility), not from a data-feed bar
    # -- it doesn't fit the bar-fill-queue machinery _execute_fill is built
    # around, so forcing it in there risked exactly the kind of subtle
    # cash/state bug force_close_all had before it was fixed. This reuses
    # only the cash ledger (self.cash) and the trade-log pattern (Trade ->
    # OptionTrade), not the Order/fill-queue path.
    #
    # Volatility is supplied by the CALLER (a strategy computes it from its
    # own price history via volatility.py) and fixed for that position's
    # entire life -- see OptionPosition's docstring for why that's a
    # stated simplification, not an oversight.
    #
    # No pyramiding restriction here unlike equities: a strategy can hold
    # multiple option positions on the same underlying at once (e.g. a
    # straddle's call + put legs), since each gets its own position_id.

    def write_option(self, underlying: str, strike: float, expiry: datetime, option_type: OptionType,
                      lots: float, spot: float, volatility: float, lot_size: float = 1.0,
                      tag: str = "entry") -> str:
        """Sell to open (the writer collects premium now, and profits if it decays toward zero)."""
        return self._open_option_position(underlying, strike, expiry, option_type, lots, spot,
                                           volatility, lot_size, side=OrderSide.SELL, tag=tag)

    def buy_option(self, underlying: str, strike: float, expiry: datetime, option_type: OptionType,
                   lots: float, spot: float, volatility: float, lot_size: float = 1.0,
                   tag: str = "entry") -> str:
        """Buy to open (the buyer pays premium now, and profits if the option's value rises)."""
        return self._open_option_position(underlying, strike, expiry, option_type, lots, spot,
                                           volatility, lot_size, side=OrderSide.BUY, tag=tag)

    def _open_option_position(self, underlying, strike, expiry, option_type, lots, spot,
                               volatility, lot_size, side, tag) -> str:
        if self._current_time is None:
            raise ValueError("write_option/buy_option called before any bar has been processed")
        days_to_expiry = (expiry - self._current_time).days
        if days_to_expiry < 0:
            raise ValueError(f"expiry {expiry} is already in the past as of {self._current_time}")

        premium = black_scholes(spot, strike, days_to_expiry, volatility, option_type).price
        commission = self.commission.compute(lots * lot_size, premium)
        self.cash += (premium * lots * lot_size - commission) if side == OrderSide.SELL \
            else -(premium * lots * lot_size + commission)

        position_id = f"OPT-{next(_order_ids)}"
        self._option_positions[position_id] = OptionPosition(
            position_id=position_id, underlying=underlying, strike=strike, expiry=expiry,
            option_type=option_type, side=side, lots=lots, lot_size=lot_size,
            entry_premium=premium, entry_time=self._current_time, volatility=volatility,
        )
        return position_id

    def get_option_position(self, position_id: str) -> OptionPosition | None:
        return self._option_positions.get(position_id)

    def get_open_option_positions(self, underlying: str | None = None) -> list[OptionPosition]:
        positions = list(self._option_positions.values())
        return [p for p in positions if p.underlying == underlying] if underlying else positions

    def close_option_position(self, position_id: str, spot: float, tag: str = "signal") -> OptionTrade:
        position = self._option_positions.pop(position_id)
        days_to_expiry = max((position.expiry - self._current_time).days, 0)
        exit_premium = black_scholes(spot, position.strike, days_to_expiry,
                                      position.volatility, position.option_type).price
        return self._settle_option(position, exit_premium, tag)

    def process_option_expiries(self, current_time: datetime, spot_lookup: dict[str, float]) -> None:
        """Call once per bar (engine-facing, like process_bar) to auto-settle any position whose expiry has passed."""
        for position_id, position in list(self._option_positions.items()):
            if position.expiry <= current_time and position.underlying in spot_lookup:
                spot = spot_lookup[position.underlying]
                intrinsic = max(spot - position.strike, 0.0) if position.option_type == OptionType.CALL \
                    else max(position.strike - spot, 0.0)
                del self._option_positions[position_id]
                self._settle_option(position, intrinsic, "expiry")

    def _settle_option(self, position: OptionPosition, exit_premium: float, tag: str) -> OptionTrade:
        commission = self.commission.compute(position.lots * position.lot_size, exit_premium)
        self.cash += (-(exit_premium * position.lots * position.lot_size) - commission) \
            if position.side == OrderSide.SELL else (exit_premium * position.lots * position.lot_size - commission)

        trade = OptionTrade(
            underlying=position.underlying, strike=position.strike, expiry=position.expiry,
            option_type=position.option_type, side=position.side, lots=position.lots, lot_size=position.lot_size,
            entry_time=position.entry_time, entry_premium=position.entry_premium,
            exit_time=self._current_time, exit_premium=exit_premium, commission=commission, exit_reason=tag,
        )
        self.option_trades.append(trade)
        return trade

    def _option_unrealized_value(self, current_prices: dict[str, float]) -> float:
        """
        Mark-to-market contribution of every open option position, for
        get_equity(). NOTE: this is the position's CURRENT value, not
        P&L-since-entry -- the entry premium was already moved into/out
        of self.cash at open_option_position() time, so referencing
        entry_premium again here would double-count it. A short position
        is a LIABILITY worth -current_premium (what it'd cost to buy back
        right now); a long position is an ASSET worth +current_premium.
        Caught by testing: an earlier version computed
        (entry_premium - current_premium) here, which showed a phantom
        profit at the instant a position was opened, before anything had
        even moved -- exactly the kind of bug this project tests for
        instead of trusting the first draft.
        """
        total = 0.0
        for position in self._option_positions.values():
            if position.underlying not in current_prices or self._current_time is None:
                continue
            days_to_expiry = max((position.expiry - self._current_time).days, 0)
            current_premium = black_scholes(current_prices[position.underlying], position.strike,
                                             days_to_expiry, position.volatility, position.option_type).price
            value = -current_premium if position.side == OrderSide.SELL else current_premium
            total += value * position.lots * position.lot_size
        return total

    # ---- Internal fill mechanics ---------------------------------------

    def _fill_pending_market_orders(self, bar: Bar) -> None:
        remaining = []
        for order in self._pending_market_orders:
            if order.symbol == bar.symbol:
                self._execute_fill(order, bar.open, bar.timestamp)
            else:
                remaining.append(order)
        self._pending_market_orders = remaining

    def _check_resting_orders(self, bar: Bar) -> None:
        triggered = []
        for order in list(self._resting_orders.values()):
            if order.symbol != bar.symbol:
                continue
            price = self._trigger_price(order, bar)
            if price is not None:
                triggered.append((order, price))

        for order, price in triggered:
            if order.order_id not in self._resting_orders:
                continue  # already cancelled by its OCO sibling firing first in this same bar
            self._execute_fill(order, price, bar.timestamp)
            if order.oco_group:
                self._cancel_oco_siblings(order)

    @staticmethod
    def _trigger_price(order: Order, bar: Bar) -> float | None:
        if order.order_type == OrderType.STOP:
            if order.side == OrderSide.SELL and bar.low <= order.stop_price:
                return order.stop_price
            if order.side == OrderSide.BUY and bar.high >= order.stop_price:
                return order.stop_price
        elif order.order_type == OrderType.LIMIT:
            if order.side == OrderSide.SELL and bar.high >= order.limit_price:
                return order.limit_price
            if order.side == OrderSide.BUY and bar.low <= order.limit_price:
                return order.limit_price
        return None

    def _cancel_resting_orders_for(self, symbol: str) -> None:
        for order_id in [oid for oid, o in self._resting_orders.items() if o.symbol == symbol]:
            self._resting_orders[order_id].status = OrderStatus.CANCELLED
            del self._resting_orders[order_id]

    def _cancel_oco_siblings(self, filled_order: Order) -> None:
        for order_id in list(self._resting_orders):
            sibling = self._resting_orders[order_id]
            if sibling.oco_group == filled_order.oco_group and order_id != filled_order.order_id:
                sibling.status = OrderStatus.CANCELLED
                del self._resting_orders[order_id]

    def _execute_fill(self, order: Order, raw_price: float, timestamp: datetime) -> None:
        fill_price = self.slippage.apply(raw_price, order.side)
        commission = self.commission.compute(order.quantity, fill_price)
        order.status = OrderStatus.FILLED
        self.fills.append(Fill(order=order, fill_price=fill_price, fill_time=timestamp, commission=commission))

        position = self._positions.setdefault(order.symbol, Position(symbol=order.symbol))
        signed_qty = order.quantity if order.side == OrderSide.BUY else -order.quantity

        was_flat = not position.is_open
        closes_position = position.is_open and (
            (position.is_long and order.side == OrderSide.SELL) or
            (position.is_short and order.side == OrderSide.BUY)
        )

        self.cash -= signed_qty * fill_price + commission

        if was_flat:
            position.quantity = signed_qty
            position.avg_price = fill_price
            self._entry_records[order.symbol] = {
                "side": order.side, "entry_time": timestamp,
                "entry_price": fill_price, "quantity": order.quantity, "commission": commission,
            }
            self._attach_exit_orders(order.symbol, signed_qty, order.attached_stop, order.attached_target)
        elif closes_position:
            self._close_position_at(order.symbol, fill_price, timestamp, order.tag, extra_commission=commission)
        else:
            raise ValueError(
                f"{order.symbol}: order would change position size while already open "
                f"({position.quantity} -> adding {signed_qty}) -- partial scaling isn't supported yet, "
                f"see broker.py module docstring"
            )

    def _close_position_at(self, symbol: str, exit_price: float, exit_time: datetime,
                            reason: str, extra_commission: float = 0.0) -> None:
        entry = self._entry_records.pop(symbol)
        self.trades.append(Trade(
            symbol=symbol, side=entry["side"], entry_time=entry["entry_time"],
            entry_price=entry["entry_price"], exit_time=exit_time, exit_price=exit_price,
            quantity=entry["quantity"], commission=entry["commission"] + extra_commission,
            exit_reason=reason,
        ))
        self._positions[symbol] = Position(symbol=symbol)
        self._cancel_resting_orders_for(symbol)
