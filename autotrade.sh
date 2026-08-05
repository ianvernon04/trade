#!/usr/bin/env bash
# Scheduled entry point for autonomous mode (the routine in CLAUDE.md).
#
# Not meant to be run by hand day-to-day — install-autotrade.sh points cron
# at this. Two modes:
#
#   ./autotrade.sh dry    do everything except place orders (default)
#   ./autotrade.sh live   place orders per the CLAUDE.md gates
#
# Everything is appended to autotrade.log with timestamps, so the morning
# after a run you can read exactly what happened. `python -m app report` is
# the other half of that picture.

set -uo pipefail
cd "$(dirname "$0")" || exit 1

MODE="${1:-dry}"
LOG="autotrade.log"
LOCK=".autotrade.lock"

# cron runs with a near-empty PATH — the single most common reason a
# scheduled job that works in your terminal does nothing at 9am. Claude
# Code installs to ~/.local/bin, so put it back on the path explicitly.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:$PATH"

# Extra flags passed to `claude`. Left empty on purpose.
#
# If a scheduled run hangs forever and the log shows nothing after "starting
# run", it is almost certainly waiting on an interactive tool-permission
# prompt that nobody is there to answer. Two ways out, in order of
# preference:
#   1. Pre-approve just the Robinhood tools in .claude/settings.local.json
#      (safer: you name exactly what may run unattended).
#   2. CLAUDE_FLAGS="--dangerously-skip-permissions" — approves *everything*
#      this session might do, not just trading. Understand that before using it.
CLAUDE_FLAGS="${CLAUDE_FLAGS:-}"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

case "$MODE" in
  dry|live) ;;
  *) log "ERROR: unknown mode '$MODE' (expected 'dry' or 'live') — nothing run"; exit 2 ;;
esac

# Never let a slow or stuck run overlap the next scheduled one.
if ! mkdir "$LOCK" 2>/dev/null; then
  log "SKIPPED: a previous run is still going (remove $LOCK if that's wrong)"
  exit 0
fi
trap 'rmdir "$LOCK" 2>/dev/null' EXIT

if ! command -v claude >/dev/null 2>&1; then
  log "ERROR: 'claude' not found on PATH — is Claude Code installed for this user?"
  exit 127
fi

BASE_PROMPT="Run the autonomous mode routine documented in CLAUDE.md, now.
You are running unattended on a schedule: no human is watching, and nobody
can answer a question or approve anything mid-run. Follow that section
exactly — including the circuit breaker and every gate a setup must clear.
Keep your output terse; it goes to a log file a human reads later."

DRY_SUFFIX="
DRY RUN — this run must not place, modify, or cancel any real order. Do
everything else the routine says (evaluate, report, scan, apply the gates,
log decisions and notes). When a setup clears every gate, log the proposal
and a note recording that it WOULD have traded and at what size, then move
on without calling any order tool. End by noting this was a dry run."

if [ "$MODE" = "live" ]; then
  PROMPT="$BASE_PROMPT"
else
  PROMPT="$BASE_PROMPT$DRY_SUFFIX"
fi

log "=== starting $MODE run ==="
# shellcheck disable=SC2086
claude -p "$PROMPT" $CLAUDE_FLAGS >> "$LOG" 2>&1
STATUS=$?
printf '\n' >> "$LOG"
if [ $STATUS -eq 0 ]; then
  log "=== $MODE run finished ok ==="
else
  log "=== $MODE run FAILED (exit $STATUS) — check the output above ==="
fi
exit $STATUS
