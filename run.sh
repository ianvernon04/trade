#!/usr/bin/env bash
# Start the Options Trading Assistant.
set -e
cd "$(dirname "$0")"
pip install -q -r requirements.txt
# HOST=0.0.0.0 ./run.sh  shares the app with other devices on your network.
echo "Starting on http://${HOST:-127.0.0.1}:${PORT:-8000} ..."
exec uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
