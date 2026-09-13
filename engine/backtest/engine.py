"""
Orchestrates DataFeed + Strategy + Broker through one bar-by-bar backtest.

This is the one place that knows the actual event order each timestamp
goes through -- everything else (Strategy, Broker) only knows its own
piece. Changing that order is the only way look-ahead bias could creep
in, so it's kept in this single function rather than spread across
files: for each timestamp, in order,
  1. feed every symbol's new bar to the broker (fills pending market
     orders from the PREVIOUS bar's decisions, and checks resting
     stop/limit orders against THIS bar's range) -- before the strategy
     sees this bar at all,
  2. THEN let the strategy react to this bar via on_bar(),
  3. THEN record equity.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time as dt_time

import pandas as pd

from .broker import SimulatedBroker
from .data_feed import DataFeed
from .strategy import Strategy
from .types import Bar, CommissionModel, Order, SlippageModel, Trade


@dataclass
class BacktestResult:
    strategy_name: str
    symbols: list[str]
    starting_cash: float
    trades: list[Trade]
    option_trades: list = field(default_factory=list)
    equity_curve: list[tuple[datetime, float]] = field(default_factory=list)
    # Captured right before the final force-close, i.e. what the strategy
    # wanted to do as of the LAST bar in the data but hadn't been filled on
    # yet -- this is what signals.py uses to answer "is there a live signal
    # today", by pointing `end` at today and reading these instead of
    # writing a second, separate signal-detection code path.
    pending_orders_at_end: list[Order] = field(default_factory=list)
    resting_orders_at_end: list[Order] = field(default_factory=list)

    def equity_series(self) -> pd.Series:
        if not self.equity_curve:
            return pd.Series(dtype=float)
        timestamps, values = zip(*self.equity_curve)
        return pd.Series(values, index=pd.DatetimeIndex(timestamps))


class BacktestEngine:
    def __init__(
        self,
        data_feed: DataFeed,
        symbols: list[str],
        start: str,
        end: str,
        strategy_cls: type[Strategy],
        strategy_params: dict | None = None,
        starting_cash: float = 100_000.0,
        commission: CommissionModel | None = None,
        slippage: SlippageModel | None = None,
        intraday_square_off_time: dt_time | None = None,
    ):
        """
        intraday_square_off_time: for pure-intraday styles (scalping,
        intraday momentum) that must never carry a position overnight.
        Once a bar's time-of-day reaches this value, every open position
        is force-closed for the day and the strategy stops receiving
        on_bar() calls until the next day's first bar -- so a strategy
        doesn't need to re-implement "don't hold past 3:20pm" logic
        itself. Leave None for swing/position styles that should hold
        across sessions.
        """
        self.data_feed = data_feed
        self.symbols = symbols
        self.start = start
        self.end = end
        self.strategy_cls = strategy_cls
        self.strategy_params = strategy_params or {}
        self.starting_cash = starting_cash
        self.commission = commission
        self.slippage = slippage
        self.intraday_square_off_time = intraday_square_off_time

    def run(self) -> BacktestResult:
        data = self.data_feed.load(self.symbols, self.start, self.end)

        broker = SimulatedBroker(self.starting_cash, self.commission, self.slippage)
        strategy = self.strategy_cls(self.symbols, **self.strategy_params)
        strategy.on_start(broker)

        all_timestamps = sorted(set().union(*(df.index for df in data.values())))
        last_price: dict[str, float] = {}
        current_day = None
        squared_off_today = False

        for ts in all_timestamps:
            if ts.date() != current_day:
                current_day = ts.date()
                squared_off_today = False

            bars_at_ts: dict[str, Bar] = {}
            for symbol, df in data.items():
                if ts not in df.index:
                    continue
                row = df.loc[ts]
                bar = Bar(
                    symbol=symbol, timestamp=ts,
                    open=float(row["open"]), high=float(row["high"]),
                    low=float(row["low"]), close=float(row["close"]),
                    volume=float(row["volume"]),
                )
                broker.process_bar(bar)
                last_price[symbol] = bar.close
                bars_at_ts[symbol] = bar

            broker.process_option_expiries(ts, last_price)

            if (self.intraday_square_off_time is not None and not squared_off_today
                    and ts.time() >= self.intraday_square_off_time):
                broker.force_close_all(last_price, ts, reason="intraday_square_off")
                # Same reasoning as force_close_all itself: an open OPTION
                # position at square-off time would otherwise silently carry
                # past the intraday cutoff instead of actually closing --
                # wrong for any same-day-only options strategy.
                for position_id in list(broker._option_positions):
                    position = broker._option_positions[position_id]
                    if position.underlying in last_price:
                        broker.close_option_position(position_id, last_price[position.underlying],
                                                      tag="intraday_square_off")
                squared_off_today = True

            if bars_at_ts and not squared_off_today:
                history = {symbol: data[symbol].loc[:ts] for symbol in bars_at_ts}
                strategy.on_bar(ts, history, broker)

            broker.equity_curve.append((ts, broker.get_equity(last_price)))

        pending_orders_at_end = list(broker._pending_market_orders)
        resting_orders_at_end = list(broker._resting_orders.values())

        if all_timestamps:
            final_ts = all_timestamps[-1]
            broker.force_close_all(last_price, final_ts, reason="end_of_backtest")
            # Same reasoning as force_close_all above, applied to options: an
            # option position still open at the end would otherwise just
            # vanish from the trade log while its mark-to-market value
            # silently drops out of the final equity number.
            for position_id in list(broker._option_positions):
                position = broker._option_positions[position_id]
                if position.underlying in last_price:
                    broker.close_option_position(position_id, last_price[position.underlying],
                                                  tag="end_of_backtest")
            broker.equity_curve[-1] = (final_ts, broker.get_equity(last_price))

        strategy.on_end(broker)

        return BacktestResult(
            strategy_name=strategy.__class__.__name__,
            symbols=self.symbols,
            starting_cash=self.starting_cash,
            trades=broker.trades,
            option_trades=broker.option_trades,
            equity_curve=broker.equity_curve,
            pending_orders_at_end=pending_orders_at_end,
            resting_orders_at_end=resting_orders_at_end,
        )
