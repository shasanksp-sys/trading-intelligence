"""
Turns a BacktestResult into the numbers you'd actually judge a strategy
by, plus a flat trade log for manual sanity-checking (see the roadmap
note: read a sample of trades by hand before trusting any metric here).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .engine import BacktestResult


def compute_metrics(result: BacktestResult, periods_per_year: int = 252) -> dict:
    trades = result.trades
    equity = result.equity_series()

    if not trades:
        return {
            "num_trades": 0,
            "note": "no trades were closed during this backtest -- nothing else to report",
        }

    pnls = np.array([t.pnl for t in trades])
    wins = pnls[pnls > 0]
    losses = pnls[pnls <= 0]

    gross_profit = wins.sum() if len(wins) else 0.0
    gross_loss = abs(losses.sum()) if len(losses) else 0.0

    metrics = {
        "num_trades": len(trades),
        "win_rate": len(wins) / len(trades),
        "avg_win": float(wins.mean()) if len(wins) else 0.0,
        "avg_loss": float(losses.mean()) if len(losses) else 0.0,
        "profit_factor": (gross_profit / gross_loss) if gross_loss > 0 else float("inf"),
        "total_pnl": float(pnls.sum()),
        "total_return_pct": (equity.iloc[-1] / result.starting_cash - 1) * 100 if len(equity) else None,
    }

    if len(equity) > 1:
        drawdown = equity / equity.cummax() - 1
        metrics["max_drawdown_pct"] = float(drawdown.min() * 100)

        returns = equity.pct_change().dropna()
        if returns.std() > 0:
            metrics["sharpe_ratio"] = float(returns.mean() / returns.std() * np.sqrt(periods_per_year))
        else:
            metrics["sharpe_ratio"] = 0.0

    return metrics


def trade_log(result: BacktestResult) -> pd.DataFrame:
    """Flat, human-readable table of every closed trade -- read this before trusting compute_metrics()."""
    rows = [{
        "symbol": t.symbol,
        "side": t.side.value,
        "entry_time": t.entry_time,
        "entry_price": t.entry_price,
        "exit_time": t.exit_time,
        "exit_price": t.exit_price,
        "quantity": t.quantity,
        "pnl": t.pnl,
        "return_pct": t.return_pct * 100,
        "exit_reason": t.exit_reason,
    } for t in result.trades]
    return pd.DataFrame(rows)
