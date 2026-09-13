"""
"Is there a live signal right now" -- reuses BacktestEngine rather than
writing separate signal-detection logic, on purpose: a second code path
computing "what would the strategy do today" that isn't the same code as
the backtest would inevitably drift from it (see broker.py's
force_close_all fix earlier this session for exactly that class of bug).
Instead, this just runs a normal backtest with `end` set to today and
reads what was left un-filled at the very end.

This is the "alert" half of alert-then-confirm semi-automation (see
telegram_alert.py for the notification side, and the roadmap memory for
why nothing here places a real order on its own): it only ever answers
"would this strategy act", never acts itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .data_feed import DataFeed
from .engine import BacktestEngine
from .strategy import Strategy
from .types import Order


@dataclass
class Signal:
    symbol: str
    strategy_name: str
    order: Order

    @property
    def description(self) -> str:
        bracket = f" (stop {self.order.attached_stop:.2f}, target {self.order.attached_target:.2f})" \
            if self.order.attached_stop or self.order.attached_target else ""
        return (f"{self.strategy_name} on {self.symbol}: {self.order.side.value} "
                f"{self.order.quantity:g} @ {self.order.order_type.value}{bracket}")


def check_latest_signal(
    strategy_cls: type[Strategy],
    symbols: list[str],
    data_feed: DataFeed,
    lookback_days: int = 400,
    strategy_params: dict | None = None,
    starting_cash: float = 1_000_000.0,
) -> list[Signal]:
    """
    Runs strategy_cls over the trailing `lookback_days` up through today,
    and returns one Signal per NEW entry order the strategy wanted to
    place but that the backtest ran out of days to fill
    (BacktestResult.pending_orders_at_end) -- i.e. exactly the entry it
    would take next if today's data feeds into a live decision.

    Deliberately excludes resting_orders_at_end (stop/target brackets on
    an already-open position): those aren't a new action needing
    confirmation, they're just the existing position's protective levels
    -- surfacing them here as if they were fresh signals would be
    misleading. Read result.resting_orders_at_end directly if you want
    "what's my current position protected at" instead of "what's new".

    lookback_days needs to be long enough for the strategy's own
    indicators to warm up (e.g. a 200-day moving average needs at least
    200 days of history before it means anything) -- 400 is a reasonable
    default for daily-bar strategies, not a universal correct number.
    """
    end = date.today()
    start = end - timedelta(days=lookback_days)

    engine = BacktestEngine(
        data_feed=data_feed, symbols=symbols, start=start.isoformat(), end=end.isoformat(),
        strategy_cls=strategy_cls, strategy_params=strategy_params, starting_cash=starting_cash,
    )
    result = engine.run()

    return [Signal(symbol=order.symbol, strategy_name=result.strategy_name, order=order)
            for order in result.pending_orders_at_end]
