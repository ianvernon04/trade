#!/usr/bin/env bash
# Install (or remove) a LaunchAgent that keeps the web app running.
#
#   ./install-server.sh                 start on 127.0.0.1:8000, restart on crash + at login
#   ./install-server.sh 8080            same, different port
#   ./install-server.sh 8000 0.0.0.0    bind all interfaces (share on your LAN)
#   ./install-server.sh --show          status + how to reach it
#   ./install-server.sh --remove        stop it and uninstall
#
# Why launchd and not `./run.sh` in a terminal: run.sh dies with the terminal
# and never comes back. This survives logout, crashes, and reboots.
#
# Deliberately NOT the same mechanism as install-autotrade.sh. That one is a
# scheduled job that runs and exits; this is a daemon that must stay up, so it
# wants KeepAlive rather than a calendar interval.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

REPO="$(pwd)"
LABEL="com.iannxavierr.trade.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# uvicorn is not on launchd's minimal PATH; resolve it now, at install time,
# so a missing dependency fails here where a human can read it rather than
# silently at login.
UVICORN="$(command -v uvicorn || true)"

# Capture first, then match. `launchctl list | grep -q` looks correct and is
# not: grep -q exits on the first match, launchctl takes SIGPIPE, and under
# `set -o pipefail` that non-zero status wins — so a job that IS loaded reports
# as missing.
is_loaded() {
  local listing
  listing="$(launchctl list 2>/dev/null)"
  case "$listing" in *"$LABEL"*) return 0 ;; *) return 1 ;; esac
}

case "${1:-}" in
  --show)
    if [ ! -f "$PLIST" ]; then echo "Not installed."; exit 0; fi
    port="$(grep -A1 -- '--port' "$PLIST" | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/')"
    host="$(grep -A1 -- '--host' "$PLIST" | tail -1 | sed 's/.*<string>\(.*\)<\/string>.*/\1/')"
    if is_loaded; then
      echo "Running — http://${host}:${port}"
      code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:${port}/" 2>/dev/null)"
      echo "  HTTP check: ${code:-no response}"
    else
      echo "Installed but not loaded (plist at $PLIST)"
    fi
    exit 0 ;;
  --remove)
    if [ ! -f "$PLIST" ]; then echo "Nothing to remove — not installed."; exit 0; fi
    launchctl unload "$PLIST" 2>/dev/null
    rm -f "$PLIST"
    echo "Removed. The web app no longer starts on its own."
    exit 0 ;;
esac

PORT="${1:-8000}"
HOST="${2:-127.0.0.1}"

if ! printf '%s' "$PORT" | grep -Eq '^[0-9]{2,5}$' || [ "$PORT" -gt 65535 ]; then
  echo "error: port must be a number under 65536 (got '$PORT')" >&2
  exit 2
fi

if [ -z "$UVICORN" ]; then
  echo "error: uvicorn not found on PATH — run 'pip install -r requirements.txt' first." >&2
  exit 127
fi

mkdir -p "$HOME/Library/LaunchAgents"

# Unload before overwriting: launchd keeps running the OLD plist otherwise, so
# re-running with a new port would leave the previous port serving too.
[ -f "$PLIST" ] && launchctl unload "$PLIST" 2>/dev/null

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>$LABEL</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UVICORN</string>
        <string>app.main:app</string>
        <string>--host</string>
        <string>$HOST</string>
        <string>--port</string>
        <string>$PORT</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$REPO/server-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>$REPO/server-stderr.log</string>
</dict>
</plist>
PLIST_EOF

if ! plutil -lint "$PLIST" >/dev/null 2>&1; then
  echo "error: generated plist is malformed — nothing loaded." >&2
  rm -f "$PLIST"
  exit 1
fi

if ! launchctl load "$PLIST" 2>&1; then
  echo "error: launchctl refused the job — see the message above." >&2
  exit 1
fi

# Give uvicorn a moment, then prove it actually answers rather than just
# claiming success because launchctl exited 0.
for _ in 1 2 3 4 5 6 7 8 9 10; do
  code="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" 2>/dev/null)"
  [ "$code" = "200" ] && break
  sleep 1
done

echo "Installed. The web app runs at http://$HOST:$PORT"
if [ "${code:-}" = "200" ]; then
  echo "  Verified: HTTP 200."
else
  echo "  WARNING: no 200 yet (got '${code:-no response}') — check $REPO/server-stderr.log"
fi
echo
echo "It starts at login and restarts if it crashes."
if [ "$HOST" = "0.0.0.0" ]; then
  echo "  Bound to 0.0.0.0 — reachable by anything on your network. Set"
  echo "  APP_PASSWORD in the environment if you want a login prompt."
fi
echo
echo "    ./install-server.sh --show     status"
echo "    ./install-server.sh --remove   stop and uninstall"
echo "    tail -20 $REPO/server-stderr.log"
