"""
Sends a signal (from signals.py) as a Telegram message -- the notification
half of alert-then-confirm semi-automation (see the video summary in
01_OUTPUT/YouTube/.../ria60hCiv0Q for the pattern this follows). This
module NEVER places an order; it only ever sends text. Confirming and
actually acting on an alert is always a separate, deliberate step the
person takes themselves (in the Streamlit UI, or directly with their
broker) -- nothing here closes that loop automatically, on purpose.

Credentials come from telegram_credentials.local.json (never share that
file -- same convention as arrow_credentials.local.json / settings.local.json).
Get a bot token from @BotFather on Telegram; get your chat_id by messaging
your new bot once, then visiting
https://api.telegram.org/bot<TOKEN>/getUpdates and reading "chat":{"id": ...}.

Degrades gracefully when not configured: send_alert() logs to a local
file and returns instead of raising, so the rest of the signal-checking
flow (and the Streamlit UI built on it) works today without needing this
credential yet -- only the actual phone notification is missing until
it's set up.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

DEFAULT_CREDENTIALS_PATH = Path(__file__).resolve().parent.parent.parent / "telegram_credentials.local.json"
FALLBACK_LOG_PATH = Path(__file__).resolve().parent.parent.parent / "logs" / "alerts_local.log"


def _load_credentials(path: Path) -> dict | None:
    if not path.exists():
        return None
    creds = json.loads(path.read_text())
    if not creds.get("bot_token") or not creds.get("chat_id"):
        return None
    return creds


def send_alert(message: str, credentials_path: str | Path = DEFAULT_CREDENTIALS_PATH) -> bool:
    """Returns True if actually sent via Telegram, False if it fell back to the local log."""
    creds = _load_credentials(Path(credentials_path))
    timestamp = datetime.now().isoformat(timespec="seconds")

    if creds is None:
        FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(FALLBACK_LOG_PATH, "a") as f:
            f.write(f"[{timestamp}] (Telegram not configured -- logged locally only) {message}\n")
        return False

    import urllib.request
    import urllib.parse

    url = f"https://api.telegram.org/bot{creds['bot_token']}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": creds["chat_id"], "text": message}).encode()
    try:
        with urllib.request.urlopen(url, data=data, timeout=10) as resp:
            return resp.status == 200
    except Exception as e:
        FALLBACK_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(FALLBACK_LOG_PATH, "a") as f:
            f.write(f"[{timestamp}] (Telegram send FAILED: {type(e).__name__}: {e}) {message}\n")
        return False
