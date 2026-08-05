#!/usr/bin/env bash
# Install (or remove) the cron entry that runs autonomous mode on a schedule.
#
#   ./install-autotrade.sh                  weekdays 9:00am, DRY RUN (default)
#   ./install-autotrade.sh live             weekdays 9:00am, places real orders
#   ./install-autotrade.sh live 15:45       weekdays 3:45pm, real orders
#   ./install-autotrade.sh --show           print the current entry
#   ./install-autotrade.sh --remove         uninstall it
#
# Re-running replaces the existing entry rather than stacking duplicates, so
# switching dry -> live is just running it again with `live`.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

REPO="$(pwd)"
MARKER="# trade-agent-autotrade"
CRONTAB_CMD="${CRONTAB_CMD:-crontab}"   # overridable so this is testable

current_crontab() { $CRONTAB_CMD -l 2>/dev/null || true; }
without_ours() { current_crontab | grep -vF "$MARKER" || true; }

case "${1:-}" in
  --show)
    line="$(current_crontab | grep -F "$MARKER" || true)"
    if [ -n "$line" ]; then echo "$line"; else echo "No autotrade entry installed."; fi
    exit 0 ;;
  --remove)
    if ! current_crontab | grep -qF "$MARKER"; then
      echo "Nothing to remove — no autotrade entry installed."
      exit 0
    fi
    REMAINING="$(without_ours)"   # read fully before writing (see note below)
    printf '%s\n' "$REMAINING" | $CRONTAB_CMD -
    echo "Removed. The agent will no longer run on a schedule."
    exit 0 ;;
esac

MODE="${1:-dry}"
AT="${2:-09:00}"

case "$MODE" in
  dry|live) ;;
  *) echo "error: mode must be 'dry' or 'live' (got '$MODE')" >&2; exit 2 ;;
esac

if ! printf '%s' "$AT" | grep -Eq '^[0-2]?[0-9]:[0-5][0-9]$'; then
  echo "error: time must look like 09:00 or 15:45 (got '$AT')" >&2
  exit 2
fi
HH="${AT%%:*}"; MM="${AT##*:}"
HH="$((10#$HH))"; MM="$((10#$MM))"
if [ "$HH" -gt 23 ]; then echo "error: hour out of range in '$AT'" >&2; exit 2; fi

if ! command -v "$CRONTAB_CMD" >/dev/null 2>&1; then
  echo "error: '$CRONTAB_CMD' not found — can't install a schedule on this system." >&2
  exit 127
fi

chmod +x "$REPO/autotrade.sh" 2>/dev/null

LINE="$MM $HH * * 1-5 cd $REPO && ./autotrade.sh $MODE >> $REPO/cron-stderr.log 2>&1 $MARKER"

# Build the whole new crontab in memory BEFORE writing any of it. Reading
# and writing in one pipeline ({ crontab -l; ...; } | crontab -) races: the
# writer can truncate the table before the reader has read it, silently
# destroying unrelated cron entries.
NEW_TAB="$( { without_ours; echo "$LINE"; } )"
if ! printf '%s\n' "$NEW_TAB" | $CRONTAB_CMD -; then
  echo "error: crontab refused the new entry — nothing changed." >&2
  exit 1
fi

echo "Installed. Autonomous mode runs weekdays at $(printf '%02d:%02d' "$HH" "$MM") in $MODE mode."
echo
if [ "$MODE" = "dry" ]; then
  echo "DRY RUN: it will analyze, apply every gate, and log what it WOULD have"
  echo "traded — without placing a single real order. Watch a few runs, then:"
  echo "    ./install-autotrade.sh live"
else
  echo "LIVE: this places real orders with real money, unattended, under the"
  echo "gates in CLAUDE.md. Back to safe mode any time with:"
  echo "    ./install-autotrade.sh dry"
fi
echo
echo "Check on it:"
echo "    tail -40 $REPO/autotrade.log     what the last run did"
echo "    python3 -m app report            decisions + track record"
echo "    ./install-autotrade.sh --show    confirm the schedule"
echo "    ./install-autotrade.sh --remove  stop it entirely"
echo
echo "Note: cron only fires while your Mac is awake. If macOS asks whether"
echo "cron may access your files, say yes — otherwise runs fail silently."
