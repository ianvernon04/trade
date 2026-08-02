#!/usr/bin/env bash
# Start the Options Trading Assistant.
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
echo "Starting on http://127.0.0.1:8000 ..."
exec uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
