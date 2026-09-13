"""
Instrument spot-analysis: gap and range behavior from real historical data.

Improvised from a one-off exercise in a processed YouTube video
(01_OUTPUT/YouTube/.../ria60hCiv0Q -- Ankit Rai demoing an AI-built trading
terminal): he ran an ad hoc "5-year gap-up/gap-down/range" prompt once, by
hand, for one instrument, as a gut-check before building a strategy. That's
a good instinct but a one-off script is easy to skip under time pressure --
this makes it a standing, reusable step instead.

The actual point: a strategy spec's numeric fields (stop_loss, target) are
meaningless in isolation. A "50-point stop" on NIFTY only means something
once you know NIFTY's typical daily range is ~185 points -- otherwise you
can't tell whether a stop is realistic or will just get clipped by ordinary
noise. This is meant to run BEFORE formalizing any strategy's numbers,
during Phase A (resolving a strategy's open questions into precise rules).
"""

from __future__ import annotations

import pandas as pd


def compute_spot_stats(df: pd.DataFrame, years: int = 5, gap_threshold_pct: float = 0.1) -> dict:
    """
    df: OHLCV DataFrame (DatetimeIndex, ascending) -- e.g. from CSVDataFeed/
    YFinanceDataFeed's load(). Returns gap and range statistics over the
    trailing `years` years of data actually available (silently uses less
    if the history is shorter -- callers should check len(df) if that
    distinction matters for their purpose).
    """
    cutoff = df.index.max() - pd.DateOffset(years=years)
    window = df[df.index >= cutoff].copy()

    prev_close = window["close"].shift(1)
    gap_pct = (window["open"] - prev_close) / prev_close * 100
    range_pct = (window["high"] - window["low"]) / window["open"] * 100
    range_pts = window["high"] - window["low"]

    gap_up = gap_pct[gap_pct > gap_threshold_pct]
    gap_down = gap_pct[gap_pct < -gap_threshold_pct]

    n = len(window)
    return {
        "period_start": window.index.min(),
        "period_end": window.index.max(),
        "num_sessions": n,
        "gap_up_pct_of_days": len(gap_up) / n * 100,
        "gap_up_avg_pct": gap_up.mean() if len(gap_up) else 0.0,
        "gap_up_max_pct": gap_up.max() if len(gap_up) else 0.0,
        "gap_down_pct_of_days": len(gap_down) / n * 100,
        "gap_down_avg_pct": gap_down.mean() if len(gap_down) else 0.0,
        "gap_down_max_pct": gap_down.min() if len(gap_down) else 0.0,
        "range_median_pct": range_pct.median(),
        "range_mean_pct": range_pct.mean(),
        "range_p90_pct": range_pct.quantile(0.9),
        "range_max_pct": range_pct.max(),
        "range_median_pts": range_pts.median(),
        "range_mean_pts": range_pts.mean(),
    }


def check_stop_target_realism(stats: dict, last_price: float, stop_pts: float | None = None,
                               target_pts: float | None = None) -> list[str]:
    """
    The actual improvement over the video's one-off version: turns the
    spot-analysis into an automatic sanity check against a strategy's
    stated numbers, instead of relying on a human eyeballing two printed
    tables and remembering to compare them.
    """
    warnings = []
    median_range = stats["range_median_pts"]

    if stop_pts is not None:
        if stop_pts < median_range * 0.5:
            warnings.append(
                f"stop_loss ({stop_pts:.0f} pts) is less than half the median daily range "
                f"({median_range:.0f} pts) -- likely to be clipped by ordinary daily noise, "
                f"not genuine adverse moves."
            )
        elif stop_pts > stats["range_p90_pct"] / 100 * last_price * 2:
            warnings.append(
                f"stop_loss ({stop_pts:.0f} pts) is very wide relative to typical daily range -- "
                f"check this isn't masking a poorly-defined entry rather than a deliberate choice."
            )

    if target_pts is not None and stop_pts is not None and target_pts < stop_pts:
        warnings.append(
            f"target ({target_pts:.0f} pts) is smaller than stop_loss ({stop_pts:.0f} pts) -- "
            f"this strategy needs a well-above-50% win rate just to break even; confirm that's intended."
        )

    return warnings


def print_spot_report(symbol: str, stats: dict) -> None:
    print(f"=== {symbol}: {stats['period_start'].date()} to {stats['period_end'].date()} "
          f"({stats['num_sessions']} sessions) ===")
    print(f"Gap-up days:   {stats['gap_up_pct_of_days']:.1f}% "
          f"(avg {stats['gap_up_avg_pct']:.2f}%, max {stats['gap_up_max_pct']:.2f}%)")
    print(f"Gap-down days: {stats['gap_down_pct_of_days']:.1f}% "
          f"(avg {stats['gap_down_avg_pct']:.2f}%, max {stats['gap_down_max_pct']:.2f}%)")
    print(f"Daily range: median {stats['range_median_pct']:.2f}% ({stats['range_median_pts']:.0f} pts), "
          f"90th pct {stats['range_p90_pct']:.2f}%, max {stats['range_max_pct']:.2f}%")
