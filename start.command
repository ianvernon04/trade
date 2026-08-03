#!/usr/bin/env bash
# Options Trading Assistant - macOS launcher. Double-click to run.
cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 was not found. Install it from https://www.python.org/downloads/ and run this again."
  read -r -p "Press Enter to close..."
  exit 1
fi

echo "Installing dependencies (first run takes a minute)..."
python3 -m pip install -q -r requirements.txt

echo "Starting the app... your browser will open at http://127.0.0.1:8000"
(sleep 3; open "http://127.0.0.1:8000" 2>/dev/null || xdg-open "http://127.0.0.1:8000" 2>/dev/null) &
exec python3 -m uvicorn app.main:app --port 8000
