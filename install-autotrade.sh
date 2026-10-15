#!/usr/bin/env bash
# Install (or remove) the schedule that runs autonomous mode.
#
# Two schedulers. On the owner's Mac the real one is launchd (cron on macOS
# is a compatibility shim that does not survive reboots cleanly and has no
# StandardOutPath); cron remains for Linux boxes and for tests.
#
#   ./install-autotrade.sh --launchd              launchd, weekdays 9:00am, DRY RUN
#   ./install-autotrade.sh --launchd live         launchd, weekdays 9:00am, real orders
#   ./install-autotrade.sh --launchd live 15:45   launchd, weekdays 3:45pm
#   ./install-autotrade.sh --launchd --show       print the plist and its load state
#   ./install-autotrade.sh --launchd --remove     unload and delete it
#
#   ./install-autotrade.sh                        cron, weekdays 9:00am, DRY RUN (default)
#   ./install-autotrade.sh live [HH:MM]           cron, real orders
#   ./install-autotrade.sh --show | --remove      cron: print / uninstall
#
# Re-running replaces the existing entry rather than stacking duplicates, so
# switching dry -> live is just running it again with `live`.
#
# Times are in the Mac's LOCAL zone. autotrade.sh refuses to run unless that
# zone is Central (CDT/CST), so keep the Mac on America/Chicago wherever it
# physically is — see docs/unattended-permissions.md "Travelling".
#
# The 08:30 preflight (scripts/preflight.sh) is NOT installed here; the
# plist for it is documented in docs/unattended-permissions.md.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

REPO="$(pwd)"
MARKER="# trade-agent-autotrade"
CRONTAB_CMD="${CRONTAB_CMD:-crontab}"   # overridable so this is testable
LAUNCHD_LABEL="com.iannxavierr.trade.autotrade"
LAUNCH_AGENTS="${LAUNCH_AGENTS_DIR:-$HOME/Library/LaunchAgents}"
PLIST="$LAUNCH_AGENTS/$LAUNCHD_LABEL.plist"
LAUNCHCTL="${LAUNCHCTL_CMD:-launchctl}"

usage_time() {
  AT="$1"
  if ! printf '%s' "$AT" | grep -Eq '^[0-2]?[0-9]:[0-5][0-9]$'; then
    echo "error: time must look like 09:00 or 15:45 (got '$AT')" >&2
    exit 2
  fi
  HH="${AT%%:*}"; MM="${AT##*:}"
  HH="$((10#$HH))"; MM="$((10#$MM))"
  if [ "$HH" -gt 23 ]; then echo "error: hour out of range in '$AT'" >&2; exit 2; fi
}

check_mode() {
  case "$1" in
    dry|live) ;;
    *) echo "error: mode must be 'dry' or 'live' (got '$1')" >&2; exit 2 ;;
  esac
}

after_install() {
  echo
  if [ "$MODE" = "dry" ]; then
    echo "DRY RUN: it will analyze, apply every gate, and log what it WOULD have"
    echo "traded — without placing a single real order. Watch a few runs, then"
    echo "re-run this script with 'live'."
  else
    echo "LIVE: this places real orders with real money, unattended, under the"
    echo "gates in CLAUDE.md. Back to safe mode any time by re-running with 'dry'."
  fi
  echo
  echo "Check on it:"
  echo "    tail -40 $REPO/autotrade.log     what the last run did"
  echo "    cat $REPO/preflight.status       this morning's auth check"
  echo "    python3 -m app report            decisions + track record"
  echo
  echo "Before the first unattended run, read docs/unattended-permissions.md:"
  echo "the token in ~/.config/trade/env is what keeps it alive for a year."
}

# =========================== launchd ========================================
if [ "${1:-}" = "--launchd" ]; then
  shift
  case "${1:-}" in
    --show)
      if [ -f "$PLIST" ]; then
        cat "$PLIST"
        echo
        $LAUNCHCTL list 2>/dev/null | grep -F "$LAUNCHD_LABEL" || echo "(not loaded)"
      else
        echo "No launchd entry installed ($PLIST)."
      fi
      exit 0 ;;
    --remove)
      if [ ! -f "$PLIST" ]; then
        echo "Nothing to remove — no launchd entry installed."
        exit 0
      fi
      $LAUNCHCTL bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || $LAUNCHCTL unload "$PLIST" 2>/dev/null || true
      rm -f "$PLIST"
      echo "Removed. The agent will no longer run on a schedule."
      exit 0 ;;
  esac

  MODE="${1:-dry}"
  check_mode "$MODE"
  usage_time "${2:-09:00}"

  chmod +x "$REPO/autotrade.sh" 2>/dev/null
  mkdir -p "$LAUNCH_AGENTS"

  # Weekday array rather than one Weekday key so one plist covers Mon-Fri.
  # PATH is set inside autotrade.sh; launchd gives it almost nothing.
  {
    cat <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/autotrade.sh</string>
    <string>$MODE</string>
  </array>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>StartCalendarInterval</key>
  <array>
EOF
    for wd in 1 2 3 4 5; do
      printf '    <dict><key>Weekday</key><integer>%s</integer><key>Hour</key><integer>%s</integer><key>Minute</key><integer>%s</integer></dict>\n' "$wd" "$HH" "$MM"
    done
    cat <<EOF
  </array>
  <key>StandardOutPath</key><string>$REPO/launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>$REPO/launchd-stderr.log</string>
</dict>
</plist>
EOF
  } > "$PLIST"

  if command -v plutil >/dev/null 2>&1 && ! plutil -lint -s "$PLIST"; then
    echo "error: generated plist failed plutil -lint — nothing loaded." >&2
    exit 1
  fi
  # bootout/bootstrap is the modern pair; unload/load is the fallback on
  # older launchctl. Either way, a reload picks up the new mode/time.
  $LAUNCHCTL bootout "gui/$(id -u)" "$PLIST" 2>/dev/null || $LAUNCHCTL unload "$PLIST" 2>/dev/null || true
  if ! $LAUNCHCTL bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null; then
    if ! $LAUNCHCTL load "$PLIST"; then
      echo "error: launchctl refused the plist — it is written at $PLIST but not loaded." >&2
      exit 1
    fi
  fi
  echo "Installed launchd job $LAUNCHD_LABEL: weekdays at $(printf '%02d:%02d' "$HH" "$MM") (Mac local time) in $MODE mode."
  after_install
  echo
  echo "Note: launchd fires only while the Mac is awake and logged in. A missed"
  echo "fire is NOT run late. See docs/unattended-permissions.md for pmset."
  exit 0
fi

# ============================= cron =========================================
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
check_mode "$MODE"
usage_time "${2:-09:00}"

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
after_install
echo
echo "Note: cron only fires while your Mac is awake. If macOS asks whether"
echo "cron may access your files, say yes — otherwise runs fail silently."
echo "On a Mac, prefer: ./install-autotrade.sh --launchd $MODE"
