"""
Pulls historical OHLCV via yfinance and saves it as local CSVs under
market_data/ (project root), in the exact shape CSVDataFeed expects
(<SYMBOL>.csv, a 'date' column, lowercase OHLCV columns). This is what
turns "hit the network every backtest run" into a one-time download you
can rerun periodically to refresh.

NSE symbol notes (Indian market):
  - Indices use a caret ticker on Yahoo: NIFTY 50 = ^NSEI, BANK NIFTY = ^NSEBANK.
  - Individual NSE stocks need a '.NS' suffix, e.g. RELIANCE.NS, TCS.NS.
  - Daily data goes back to ~2007 for the two indices (verified). Intraday
    (5m/15m/1h) is capped to roughly the last 60 days by Yahoo regardless
    of interval, and 1-minute bars to the last ~7 days -- nowhere near
    enough history for a meaningful intraday/scalping backtest. That's a
    Yahoo limitation, not something this script can work around; a real
    intraday backtest needs a broker historical API or a paid vendor.

Usage:
    python3 engine/backtest/fetch_market_data.py                    # default: NIFTY + BANKNIFTY, daily, max history
    python3 engine/backtest/fetch_market_data.py --symbols RELIANCE.NS,TCS.NS --interval 1d --period max
    python3 engine/backtest/fetch_market_data.py --symbols ^NSEI --interval 5m --period 60d --out-name NIFTY_5m
"""

import argparse
import sys
from pathlib import Path

MARKET_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "market_data"

DEFAULT_SYMBOLS = {
    "NIFTY": "^NSEI",
    "BANKNIFTY": "^NSEBANK",
}


def fetch_and_save(yf_ticker: str, out_name: str, interval: str, period: str) -> Path:
    import yfinance as yf

    df = yf.download(yf_ticker, period=period, interval=interval, auto_adjust=True, progress=False)
    if df.empty:
        raise ValueError(f"yfinance returned no data for {yf_ticker} (interval={interval}, period={period})")
    if hasattr(df.columns, "get_level_values"):
        df.columns = [c[0] for c in df.columns]
    df.columns = [c.lower() for c in df.columns]
    df.index.name = "date"

    MARKET_DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MARKET_DATA_DIR / f"{out_name}.csv"
    df[["open", "high", "low", "close", "volume"]].to_csv(out_path)
    return out_path, len(df), df.index.min(), df.index.max()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--symbols", default=None,
                         help="Comma-separated Yahoo tickers (e.g. ^NSEI,RELIANCE.NS). Default: NIFTY + BANKNIFTY.")
    parser.add_argument("--interval", default="1d")
    parser.add_argument("--period", default="max", help="e.g. max, 5y, 60d (60d is Yahoo's intraday cap)")
    parser.add_argument("--out-name", default=None, help="Override output filename (single-symbol runs only)")
    args = parser.parse_args()

    if args.symbols:
        tickers = args.symbols.split(",")
        targets = {(args.out_name or t.replace("^", "").replace(".", "_")): t for t in tickers}
    else:
        targets = DEFAULT_SYMBOLS

    for out_name, ticker in targets.items():
        try:
            path, n_bars, start, end = fetch_and_save(ticker, out_name, args.interval, args.period)
            print(f"{ticker} -> {path}  ({n_bars} bars, {start} to {end})")
        except Exception as e:
            print(f"FAILED {ticker}: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
