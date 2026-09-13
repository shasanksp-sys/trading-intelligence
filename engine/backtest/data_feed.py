"""
Historical data sources for backtesting.

A DataFeed's only job is producing one pandas DataFrame per symbol
(columns: open, high, low, close, volume; index: datetime, ascending,
no gaps assumed) for a given date range. The engine never reads a data
source directly -- it only calls DataFeed.load() -- so adding a new
source (a broker's historical endpoint, a Parquet file, a different
vendor) later never touches the engine or any strategy.

This intentionally mirrors the same "swap the adapter, keep the logic"
shape as the broker seam in broker.py -- see that file's docstring.
"""

from __future__ import annotations

import warnings
from abc import ABC, abstractmethod
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = ("open", "high", "low", "close", "volume")


def _validate(df: pd.DataFrame, symbol: str) -> pd.DataFrame:
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{symbol}: data is missing required column(s) {missing}")
    df = df.sort_index()
    if df.index.has_duplicates:
        raise ValueError(f"{symbol}: duplicate timestamps in historical data")

    # Found in practice (2026-09-12): Yahoo's continuous-futures data (gold,
    # natural gas) has a small fraction of bars with internally impossible
    # OHLC (e.g. high < low), almost always tiny rounding noise from how the
    # contract-roll is stitched together -- not fabricated data, but not
    # trustworthy at face value either. Warn rather than silently trust or
    # hard-fail: a handful of noisy bars in years of daily data has minor
    # effect on a swing backtest, but this should never be invisible, and a
    # real go-live decision should be backed by vendor/broker data, not this.
    bad = ((df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"]) |
           (df["low"] > df["open"]) | (df["low"] > df["close"]))
    if bad.any():
        warnings.warn(
            f"{symbol}: {bad.sum()} of {len(df)} bars have internally inconsistent OHLC "
            f"(e.g. high < low) -- known data-quality noise in this source, not a fabrication. "
            f"Don't treat results from this data as final without verifying against a real "
            f"vendor/broker feed first.",
            stacklevel=3,
        )

    return df[list(REQUIRED_COLUMNS)]


class DataFeed(ABC):
    @abstractmethod
    def load(self, symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        """Return {symbol: OHLCV DataFrame} for the given date range (inclusive)."""


class CSVDataFeed(DataFeed):
    """
    Reads one CSV per symbol from a directory, named '<SYMBOL>.csv', with
    a date/datetime column plus open/high/low/close/volume (any case).
    This is the escape hatch for any data source not covered by a
    dedicated feed below -- export to CSV in this shape and it works.
    """

    def __init__(self, directory: str, date_column: str = "date"):
        self.directory = Path(directory)
        self.date_column = date_column

    def load(self, symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        result = {}
        for symbol in symbols:
            path = self.directory / f"{symbol}.csv"
            if not path.exists():
                raise FileNotFoundError(f"No CSV found for {symbol} at {path}")
            df = pd.read_csv(path)
            df.columns = [c.lower() for c in df.columns]
            df[self.date_column] = pd.to_datetime(df[self.date_column])
            df = df.set_index(self.date_column)
            if df.index.tz is not None:
                # The engine works with naive timestamps throughout; some
                # sources (Arrow's API, confirmed live) return tz-aware
                # ones (+05:30). Strip the tz rather than localize start/end
                # instead, since a mix of naive/aware CSVs across different
                # sources needs one consistent rule, not a per-source patch.
                df.index = df.index.tz_localize(None)
            df = df.loc[(df.index >= pd.Timestamp(start)) & (df.index <= pd.Timestamp(end))]
            result[symbol] = _validate(df, symbol)
        return result


class YFinanceDataFeed(DataFeed):
    """
    Pulls daily (or intraday, subject to yfinance's own history limits on
    intraday ranges) OHLCV via the yfinance package. Good for prototyping
    on equities/ETFs/indices; not a substitute for a broker/vendor feed
    once a strategy is actually close to live (see PROJECT roadmap notes
    on data-quality requirements -- splits/dividends adjustment here comes
    from yfinance's 'auto_adjust', not independently verified).
    """

    def __init__(self, interval: str = "1d"):
        self.interval = interval

    def load(self, symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        try:
            import yfinance as yf
        except ImportError as exc:
            raise ImportError(
                "yfinance is not installed -- run: pip3 install yfinance"
            ) from exc

        result = {}
        for symbol in symbols:
            df = yf.download(
                symbol, start=start, end=end, interval=self.interval,
                auto_adjust=True, progress=False,
            )
            if df.empty:
                raise ValueError(f"yfinance returned no data for {symbol} in [{start}, {end}]")
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = [c[0] for c in df.columns]
            df.columns = [c.lower() for c in df.columns]
            result[symbol] = _validate(df, symbol)
        return result
