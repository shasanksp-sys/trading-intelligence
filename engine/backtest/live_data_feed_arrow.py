"""
DataFeed backed by a real Arrow (arrow.trade) account -- the same
DataFeed interface CSVDataFeed/YFinanceDataFeed implement, so this drops
into BacktestEngine with zero changes anywhere else. Built against the
`pyarrow-client` PyPI package (confirmed real: NOT the same as `pyarrow`,
the unrelated Apache Arrow library) and Arrow's published REST docs
(docs.arrow.trade) as of 2026-09-12.

Needs Python 3.10+ -- the installed pyarrow_client package uses `X | None`
type hints that raise a TypeError importing under this Mac's system
Python 3.9. Same reason dashboard.py's launcher pins python3.11; do the
same here (see Launch_Backtest_Frontend.command).

Credentials come from arrow_credentials.local.json (never share that
file -- same convention as settings.local.json for the Anthropic key).
Fill it in yourself; nothing here prompts for or transmits it anywhere
but Arrow's own login endpoint.

UNVERIFIED AGAINST A LIVE ACCOUNT as of first write -- built directly
from pyarrow_client's real installed method signatures (inspect.signature,
not guessed) and Arrow's own docs, but no real credentials were available
to actually exercise login or a candle_data call end-to-end. Treat the
first real run as the actual test, and expect to adjust
_resolve_token()'s parsing once you see get_instruments()/get_index_list()'s
real output shape -- their exact structure isn't documented.

Arrow doesn't publish a maximum date range per request (their own docs
just say "consider breaking requests into smaller time periods"), so
this always chunks requests defensively rather than guessing a limit and
risking a silent truncation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

from .data_feed import DataFeed, _validate

DEFAULT_CREDENTIALS_PATH = Path(__file__).resolve().parent.parent.parent / "arrow_credentials.local.json"

# Arrow's own docs never publish a max range per request -- "day" below
# is EMPIRICALLY CONFIRMED against a real live account (2026-09-13):
# binary-searched the boundary directly, 1800 days succeeds, 1805 fails.
# Every other interval here is still an unverified conservative guess
# (chunking regardless of the real limit means a request never silently
# returns a truncated range instead of erroring, so a wrong guess costs
# extra requests, not wrong data) -- narrow those the same way once
# there's a reason to pull real intraday history through this feed.
CHUNK_DAYS = {
    "min": 25, "3min": 60, "5min": 90, "10min": 90, "15min": 90, "30min": 180,
    "hour": 180, "2hours": 180, "3hours": 180, "4hours": 180,
    "day": 1800, "week": 1800, "month": 1800,
}

# Symbols this feed knows how to resolve without a full instrument-master
# scan -- extend as needed once get_index_list()'s real shape is confirmed.
KNOWN_INDEX_NAMES = {
    "NIFTY": "NIFTY 50", "BANKNIFTY": "NIFTY BANK", "FINNIFTY": "NIFTY FIN SERVICE",
    "MIDCAPNIFTY": "NIFTY MIDCAP 50", "SENSEX": "SENSEX",
}


def _load_credentials(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found -- copy arrow_credentials.local.json's template and fill in your "
            f"own app_id/api_secret/user_id/password/totp_secret. Never share this file."
        )
    creds = json.loads(path.read_text())
    missing = [k for k in ("app_id", "api_secret", "user_id", "password", "totp_secret") if not creds.get(k)]
    if missing:
        raise ValueError(f"{path}: missing/empty field(s) {missing} -- fill in arrow_credentials.local.json first")
    return creds


class ArrowDataFeed(DataFeed):
    def __init__(self, credentials_path: str | Path = DEFAULT_CREDENTIALS_PATH, interval: str = "day"):
        self.interval = interval
        try:
            from pyarrow_client import ArrowClient
        except ImportError as exc:
            raise ImportError(
                "pyarrow-client is not installed, or you're running under Python <3.10 "
                "(it fails to import there). Run: /opt/homebrew/bin/python3.11 -m pip install pyarrow-client "
                "and run this under python3.11, same as Launch_Backtest_Frontend.command does."
            ) from exc

        creds = _load_credentials(Path(credentials_path))
        self._client = ArrowClient(app_id=creds["app_id"])
        self._client.auto_login(
            user_id=creds["user_id"], password=creds["password"],
            api_secret=creds["api_secret"], totp_secret=creds["totp_secret"],
        )
        self._instrument_cache = None

    def _resolve_token(self, symbol: str) -> tuple[str, str]:
        """
        Returns (exchange, token) for a symbol. Indices go through
        get_index_list(); anything else through get_instruments(). Both
        methods' real return shape is unconfirmed without a live account
        -- this handles the common shapes (list of dicts, or an object
        with a similar mapping) and raises a clear error otherwise rather
        than silently guessing.
        """
        from pyarrow_client import Exchange

        if symbol.upper() in KNOWN_INDEX_NAMES:
            target_name = KNOWN_INDEX_NAMES[symbol.upper()]
            indices = self._client.get_index_list()
            match = self._find_by_name(indices, target_name)
            if match:
                # Confirmed against a real live account: candle_data wants
                # exchange="NSE" for index tokens too, NOT the "INDEX" enum
                # value get_index_list()'s own naming would suggest -- that
                # guess returned a 400 from the real API; caught once real
                # credentials existed to test against, exactly the caveat
                # this file's docstring flagged as unverified from the start.
                return "NSE", match
            raise ValueError(
                f"{symbol}: expected to find '{target_name}' in get_index_list()'s output but didn't -- "
                f"its real shape needs checking against a live call; got: {type(indices)}"
            )

        if self._instrument_cache is None:
            self._instrument_cache = self._client.get_instruments()
        match = self._find_by_name(self._instrument_cache, symbol.upper())
        if match:
            return "NSE", match
        raise ValueError(
            f"{symbol}: not found via get_instruments() -- check the exact symbol spelling Arrow "
            f"expects, or get_instruments()'s output shape may need adjusting in _resolve_token()."
        )

    @staticmethod
    def _find_by_name(data, name: str):
        """Best-effort lookup across plausible shapes for an unconfirmed API response."""
        rows = data if isinstance(data, list) else getattr(data, "to_dict", lambda: [])("records") if hasattr(data, "to_dict") else []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ("name", "symbol", "tradingsymbol", "index_name"):
                if row.get(key, "").upper() == name.upper():
                    return str(row.get("token") or row.get("instrument_token") or "")
        return None

    def _fetch_chunked(self, exchange: str, token: str, interval: str, start: datetime, end: datetime) -> list:
        from pyarrow_client import Exchange
        chunk_days = CHUNK_DAYS.get(interval, 90)
        rows = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=chunk_days), end)
            result = self._client.candle_data(
                exchange=getattr(Exchange, exchange), token=token, interval=interval,
                from_timestamp=cursor.strftime("%Y-%m-%dT%H:%M:%S"),
                to_timestamp=chunk_end.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            candles = result if isinstance(result, list) else result.get("data", result.get("candles", []))
            rows.extend(candles)
            cursor = chunk_end

        # Chunk boundaries overlap by one bar (confirmed live: requesting
        # [[cursor, chunk_end]] then [[chunk_end, next_end]] returns the
        # chunk_end timestamp's bar in BOTH responses) -- dedupe by
        # timestamp (each row's first element) rather than assume either
        # endpoint is exclusive, since that assumption is exactly what
        # broke on the first real chunked request.
        seen = set()
        deduped = []
        for row in rows:
            ts = row[0]
            if ts not in seen:
                seen.add(ts)
                deduped.append(row)
        return deduped

    def load(self, symbols: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
        start_dt, end_dt = datetime.fromisoformat(start), datetime.fromisoformat(end)
        result = {}
        for symbol in symbols:
            exchange, token = self._resolve_token(symbol)
            raw = self._fetch_chunked(exchange, token, self.interval, start_dt, end_dt)
            if not raw:
                raise ValueError(f"{symbol}: Arrow returned no candles for {start} to {end} @ {self.interval}")
            df = pd.DataFrame(raw, columns=["date", "open", "high", "low", "close", "volume"])
            df["date"] = pd.to_datetime(df["date"])
            df = df.set_index("date")
            for col in ("open", "high", "low", "close"):
                df[col] = df[col] / 100.0  # Arrow returns prices *100, per their docs
            result[symbol] = _validate(df, symbol)
        return result
