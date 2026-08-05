#!/usr/bin/env bash
# Run the Options Trading Assistant in the background on macOS — no terminal
# window needed. Installs a LaunchAgent that starts the server at login,
# keeps it running, and restarts it if it crashes.
#
#   ./install-server.sh            install (or replace) and start it
#   ./install-server.sh --status   is it running?
#   ./install-server.sh --logs     tail the server log
#   ./install-server.sh --remove   stop it and uninstall
#
# The server listens on http://127.0.0.1:8000 exactly like ./run.sh does.
# Limits worth knowing: launchd only runs while this Mac is powered on and
# you are logged in; sleep pauses it. For a URL that works from anywhere
# (phone included), see the Render deployment section in README.md.
set -euo pipefail

LABEL="com.options-trading-assistant.server"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$DIR/server.log"
URL="http://127.0.0.1:8000"

# Resolved at install time, and launchd is pointed at this binary directly
# rather than at `bash -lc './run.sh'`.
#
# Two reasons. The minor one: launchd's PATH is minimal, so uvicorn has to be
# absolute anyway. The one that actually bites: when the repo lives under a
# TCC-protected directory (~/Desktop, ~/Documents, ~/Downloads), a
# launchd-spawned bash cannot exec a script inside it and dies with
# "Operation not permitted" — forever, silently, in a KeepAlive loop.
# Executing an interpreter that lives outside the protected path and pointing
# its working directory inside is allowed, so this sidesteps the whole class
# of problem without asking anyone to grant Full Disk Access.
UVICORN="$(command -v uvicorn || true)"

if [[ "$(uname)" != "Darwin" ]]; then
  echo "This installer is macOS-only (it uses launchd). On Linux, use a"
  echo "systemd unit or just run ./run.sh under tmux/screen." >&2
  exit 1
fi

status() {
  if launchctl list "$LABEL" >/dev/null 2>&1; then
    echo "LaunchAgent: loaded ($LABEL)"
  else
    echo "LaunchAgent: not installed"
  fi
  if curl -s --max-time 3 -o /dev/null "$URL/api/tracking/summary"; then
    echo "Server:      answering at $URL"
  else
    echo "Server:      NOT answering at $URL"
    [[ -f "$LOG" ]] && { echo "Last log lines:"; tail -5 "$LOG"; }
  fi
}

case "${1:-install}" in
  --status) status; exit 0 ;;
  --logs)   exec tail -n 50 -f "$LOG" ;;
  --remove)
    launchctl unload -w "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "Removed. The server is stopped and will no longer start at login."
    exit 0
    ;;
  install) ;;
  *) echo "usage: $0 [--status | --logs | --remove]" >&2; exit 2 ;;
esac

# A server already answering here is probably a ./run.sh window — two servers
# can't share the port, and launchd would loop trying to start the second.
if curl -s --max-time 3 -o /dev/null "$URL"; then
  if ! launchctl list "$LABEL" >/dev/null 2>&1; then
    echo "Something is already serving $URL (a './run.sh' terminal window?)."
    echo "Close that window first, then re-run this installer." >&2
    exit 1
  fi
fi

if [[ -z "$UVICORN" ]]; then
  echo "error: uvicorn not found on PATH — run 'pip install -r requirements.txt'" >&2
  echo "       first, then re-run this installer." >&2
  exit 127
fi

mkdir -p "$HOME/Library/LaunchAgents"
launchctl unload -w "$PLIST" 2>/dev/null || true

# Dependencies are NOT installed here the way run.sh does it — launchd should
# start a server, not run pip on every login. The uvicorn check above is the
# stand-in: if the environment can't import the app, this fails at install
# time where someone is reading the output.
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$UVICORN</string>
    <string>app.main:app</string>
    <string>--host</string>
    <string>127.0.0.1</string>
    <string>--port</string>
    <string>8000</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLIST

launchctl load -w "$PLIST"
echo "Installed. Giving the server a few seconds to start..."
for _ in 1 2 3 4 5 6 7 8 9 10 11 12; do
  sleep 2
  if curl -s --max-time 3 -o /dev/null "$URL/api/tracking/summary"; then
    echo "✓ Running in the background at $URL"
    echo "  It now starts by itself when you log in, and restarts if it crashes."
    echo "  Check on it:   ./install-server.sh --status"
    echo "  Watch logs:    ./install-server.sh --logs"
    echo "  Uninstall:     ./install-server.sh --remove"
    exit 0
  fi
done
echo "Still not answering after ~25s (first run installs dependencies, which"
echo "can take a while). Watch it come up with: ./install-server.sh --logs"
