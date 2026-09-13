#!/bin/bash
# Double-click this file (or set it as a Dock/Desktop icon) to open the
# TradingIntelligence Dashboard window -- no Terminal typing needed.
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

# CRITICAL: plain "python3" can resolve to the OLD system Python 3.9
# (with a broken/deprecated Tk that renders a blank window) instead of
# Homebrew's Python 3.11 (modern, working Tk) -- explicitly prefer 3.11,
# same fix already applied to TradingIntelligence.app's launcher.
if [ -x "/opt/homebrew/bin/python3.11" ]; then
    PYTHON="/opt/homebrew/bin/python3.11"
elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON="python3.11"
else
    PYTHON="python3"
fi

"$PYTHON" dashboard.py
