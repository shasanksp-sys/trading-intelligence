#!/bin/bash
# Double-click this file to open the Backtest/Live Trading frontend in
# your browser -- no Terminal typing needed. Separate from
# Launch_Dashboard.command (the tkinter app for the extraction pipeline).
cd "$(dirname "$0")"
export PATH="/opt/homebrew/bin:/opt/homebrew/sbin:$PATH"

if [ -x "/opt/homebrew/bin/python3.11" ]; then
    PYTHON="/opt/homebrew/bin/python3.11"
elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON="python3.11"
else
    PYTHON="python3"
fi

"$PYTHON" -m streamlit run frontend/app.py
