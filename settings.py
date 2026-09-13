"""
Local settings storage -- API key, model choices, storage-policy toggles.

Stored in settings.local.json at the project root. This file is created
at runtime and should NEVER be shared or committed anywhere -- it holds
your API key in plain text (standard practice for local desktop tools,
but worth knowing). Add it to any backup exclusions the same way you'd
exclude a password file.
"""

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / "settings.local.json"

DEFAULTS = {
    "anthropic_api_key": "",
    "tagging_model": "claude-haiku-4-5-20251001",
    "reconstruction_model": "claude-sonnet-4-5",
    "auto_delete_local_video": True,
    "auto_delete_youtube_video_default": True,
}


def load_settings() -> dict:
    if not SETTINGS_PATH.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(SETTINGS_PATH.read_text())
        merged = dict(DEFAULTS)
        merged.update(data)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(DEFAULTS)


def save_settings(settings: dict) -> None:
    current = load_settings()
    current.update(settings)
    SETTINGS_PATH.write_text(json.dumps(current, indent=2))


def get_api_key() -> str:
    return load_settings().get("anthropic_api_key", "")
