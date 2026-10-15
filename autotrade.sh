#!/usr/bin/env bash
# Scheduled entry point for autonomous mode (the routine in CLAUDE.md).
#
# Not meant to be run by hand day-to-day — launchd (or cron) fires it on
# weekdays at 9:00 AM Central. Two modes:
#
#   ./autotrade.sh dry    do everything except place orders (default)
#   ./autotrade.sh live   place orders per the CLAUDE.md gates
#
# Everything is appended to autotrade.log with timestamps, so the morning
# after a run you can read exactly what happened. `python3 -m app report` is
# the other half of that picture.
#
# Exit codes (launchd ignores them, healthchecks.io does not):
#   0    ran, or deliberately skipped (holiday/weekend)
#   1    claude run failed (auth, network, timeout, non-zero from claude)
#   2    SKIPPED — another run holds the lock, or bad mode argument
#   3    refused — Mac is not on Central time, or outside 08:35-15:30 CT
#   127  claude not on PATH
#
# Environment overrides (all optional; set them in ~/.config/trade/env):
#   CLAUDE_CODE_OAUTH_TOKEN   long-lived token from `claude setup-token`
#   ANTHROPIC_API_KEY         alternative: a Console API key
#   HEALTHCHECK_PING_URL      healthchecks.io check URL — pinged /start, ok, /fail
#   AUTOTRADE_MAX_TURNS       --max-turns for claude (default 60)
#   AUTOTRADE_TIMEOUT_SECS    hard kill after this many seconds (default 2700 = 45 min)
#   AUTOTRADE_PERMISSION_MODE --permission-mode for claude (default dontAsk)
#   AUTOTRADE_ENV_FILE        where the env file lives (default ~/.config/trade/env)
#   AUTOTRADE_NOW             TESTING ONLY: "YYYY-MM-DD HH:MM ZONE" fakes the clock
#                             for the time-zone, window and holiday checks

set -uo pipefail
cd "$(dirname "$0")" || exit 1

MODE="${1:-dry}"
LOG="autotrade.log"
LOCK=".autotrade.lock"
SETTINGS="settings.unattended.json"
PREFLIGHT_STATUS="preflight.status"
POLICY="autonomy.json"
ENV_FILE="${AUTOTRADE_ENV_FILE:-$HOME/.config/trade/env}"

# launchd and cron run with a near-empty PATH — the single most common reason
# a scheduled job that works in your terminal does nothing at 9am. Claude
# Code installs to ~/.local/bin, so put it back on the path explicitly.
# launchd/cron start with a near-empty PATH, so put the usual install dirs
# back. Appended, not prepended: an explicit PATH from the caller (or a test
# harness with a stub `claude`) must keep priority.
export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# A scheduled run must never pause to update itself or pull a new binary
# mid-trade; update by hand from a terminal.
export DISABLE_AUTOUPDATER=1

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }

# --- credentials: ~/.config/trade/env ------------------------------------
# The claude.ai login this machine uses interactively expires every few
# weeks and killed 22 straight scheduled runs (Sep-Oct 2026). A token from
# `claude setup-token` lasts a year. Keep it OUTSIDE the repo:
#
#   claude setup-token                      # browser flow, prints the token
#   mkdir -p ~/.config/trade
#   echo 'export CLAUDE_CODE_OAUTH_TOKEN=<paste>' > ~/.config/trade/env
#   chmod 600 ~/.config/trade/env
#
# ANTHROPIC_API_KEY and HEALTHCHECK_PING_URL go in the same file. Never put
# a token in this script, in .claude/, or anywhere git can see it.
if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi
HC_URL="${HEALTHCHECK_PING_URL:-}"
MAX_TURNS="${AUTOTRADE_MAX_TURNS:-60}"
TIMEOUT_SECS="${AUTOTRADE_TIMEOUT_SECS:-2700}"
PERMISSION_MODE="${AUTOTRADE_PERMISSION_MODE:-dontAsk}"

# healthchecks.io dead-man pattern: /start when we begin, bare URL on
# success, /fail on failure. If the ping never arrives, the service emails
# you — which is how you learn the Mac was asleep or the token expired.
hc_ping() {  # hc_ping start|ok|fail
  [ -n "$HC_URL" ] || return 0
  case "$1" in
    start) url="$HC_URL/start" ;;
    fail)  url="$HC_URL/fail" ;;
    *)     url="$HC_URL" ;;
  esac
  curl -fsS -m 10 --retry 3 -o /dev/null "$url" 2>/dev/null \
    || log "WARN: healthcheck ping ($1) failed"
}

case "$MODE" in
  dry|live) ;;
  *) log "ERROR: unknown mode '$MODE' (expected 'dry' or 'live') — nothing run"; exit 2 ;;
esac

# --- clock guards --------------------------------------------------------
# launchd's StartCalendarInterval is in the Mac's *local* time. If the zone
# changes (travel, or "set automatically" flips it), the 9 AM job fires at
# the wrong moment of the trading day. Refuse to run unless the Mac is on
# Central time AND the Central clock is inside the window a 9 AM run could
# plausibly be in. Exit 3 so the healthcheck fails and someone looks.
if [ -n "${AUTOTRADE_NOW:-}" ]; then
  # test hook: "2026-10-12 09:00 CDT"
  read -r NOW_DATE NOW_HM NOW_ZONE <<< "$AUTOTRADE_NOW"
  NOW_HM="${NOW_HM/:/}"
else
  NOW_ZONE="$(date +%Z)"
  NOW_DATE="$(TZ=America/Chicago date +%Y-%m-%d)"
  NOW_HM="$(TZ=America/Chicago date +%H%M)"
fi
case "$NOW_ZONE" in
  CDT|CST) ;;
  *) log "REFUSED: Mac time zone is '$NOW_ZONE', not CDT/CST — fix the zone before the next run (exit 3)"
     hc_ping fail; exit 3 ;;
esac
if [ "$((10#$NOW_HM))" -lt 835 ] || [ "$((10#$NOW_HM))" -gt 1530 ]; then
  log "REFUSED: Central time is $NOW_HM, outside the 08:35-15:30 run window (exit 3)"
  hc_ping fail; exit 3
fi

# --- holidays ------------------------------------------------------------
HOLIDAY_MSG="$(python3 scripts/nyse_holidays.py "$NOW_DATE" 2>&1)"
case $? in
  0) ;;
  1) log "SKIPPED (market closed): $HOLIDAY_MSG"; hc_ping ok; exit 0 ;;
  *) log "WARN: holiday check inconclusive ($HOLIDAY_MSG) — running anyway" ;;
esac

# --- lock ----------------------------------------------------------------
# Never let a slow or stuck run overlap the next scheduled one. The lock is
# a directory (mkdir is atomic) holding the owner's pid, so a crash that
# leaves it behind is detected instead of silently skipping every later day.
take_lock() { mkdir "$LOCK" 2>/dev/null && echo $$ > "$LOCK/pid"; }
if ! take_lock; then
  OLD_PID="$(cat "$LOCK/pid" 2>/dev/null || true)"
  if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
    log "SKIPPED: a previous run (pid $OLD_PID) is still going (exit 2)"
    hc_ping fail; exit 2
  fi
  log "WARN: stale lock from pid '${OLD_PID:-?}' (not running) — removing it"
  rm -rf "$LOCK"
  if ! take_lock; then
    log "SKIPPED: could not take $LOCK even after clearing a stale one (exit 2)"
    hc_ping fail; exit 2
  fi
fi

# Restore things on every exit path. POLICY_MODE is set later if we chmod.
POLICY_MODE=""
cleanup() {
  if [ -n "$POLICY_MODE" ]; then chmod "$POLICY_MODE" "$POLICY" 2>/dev/null; fi
  rm -rf "$LOCK"
}
trap cleanup EXIT

if ! command -v claude >/dev/null 2>&1; then
  log "ERROR: 'claude' not found on PATH — is Claude Code installed for this user?"
  hc_ping fail; exit 127
fi

# --- keep the Mac awake ---------------------------------------------------
# The Mac slept mid-run three times, once while an order was being placed.
# caffeinate -i (no idle sleep) -s (no system sleep while on AC) -m (no disk
# sleep), released automatically when this script's pid exits. A closed lid
# on battery still sleeps; keep it plugged in and see docs/unattended-permissions.md.
if command -v caffeinate >/dev/null 2>&1; then
  caffeinate -ims -w $$ &
fi

# --- preflight result: refuse to go live on a known-bad morning --------------
# scripts/preflight.sh runs at 08:30 and writes "<date> <STATUS> ..." to
# preflight.status. AUTH_FAILED or MCP_NEEDS_AUTH means a live run could do
# half the routine (e.g. log a proposal) and then be unable to see the
# broker; fall back to dry and say why. A missing or stale file is only
# noted — preflight is a monitor, not a gatekeeper.
PRE_DATE=""; PRE_STATUS=""
if [ -f "$PREFLIGHT_STATUS" ]; then
  read -r PRE_DATE PRE_STATUS _ < "$PREFLIGHT_STATUS" || true
fi
if [ "$PRE_DATE" != "$NOW_DATE" ]; then
  log "INFO: no preflight result for $NOW_DATE (found: '${PRE_DATE:-none} ${PRE_STATUS:-}')"
elif [ "$MODE" = "live" ]; then
  case "$PRE_STATUS" in
    AUTH_FAILED|MCP_NEEDS_AUTH)
      log "DOWNGRADED live -> dry: preflight reported $PRE_STATUS this morning"
      MODE="dry" ;;
  esac
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

# --- run header -----------------------------------------------------------
# One glance at the log should answer "which code, which claude, which
# policy, which clock" without digging. autonomy status is collapsed to one
# line.
GIT_REV="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
CLAUDE_VER="$(claude --version 2>/dev/null | head -n 1 || echo unknown)"
POLICY_LINE="$(python3 -m app autonomy status 2>&1 | tr -s ' \n' ' ' | cut -c1-300)"
hc_ping start
log "=== starting $MODE run ==="
log "commit=$GIT_REV claude='$CLAUDE_VER' tz=$NOW_ZONE central=$NOW_DATE $NOW_HM mode=$MODE max_turns=$MAX_TURNS timeout=${TIMEOUT_SECS}s"
log "autonomy: ${POLICY_LINE:-unavailable}"

# The policy file is the owner's standing instruction; make it read-only
# for the duration so the model cannot edit it mid-run even if a rule slips.
# (Edit/Write and Bash(chmod *) are also denied in the settings file.)
if [ -f "$POLICY" ]; then
  # python rather than stat: BSD and GNU stat disagree on the flag for mode
  POLICY_MODE="$(python3 -c 'import os,sys;print(oct(os.stat(sys.argv[1]).st_mode)[-3:])' "$POLICY" 2>/dev/null)"
  chmod 444 "$POLICY"
fi

# --- claude flags ---------------------------------------------------------
# Flag references: https://code.claude.com/docs/en/cli-reference and
# https://code.claude.com/docs/en/headless (checked 2026-10-11).
#   --settings            this session's permissions, committed and reviewed
#   --setting-sources     skip .claude/settings.local.json, the interactive
#                         allowlist that accumulates "don't ask again" answers
#   --strict-mcp-config   only the Robinhood server in .mcp.json, nothing
#                         from ~/.claude.json or claude.ai connectors
#   --max-turns           a run that loops is cut off, not billed for 3 hours
#   --permission-mode dontAsk   anything that would prompt is denied
#   --permission-prompts none   (v2.1.259+) also tells the model not to retry;
#                         added only when the installed version has it
# There is deliberately no --dangerously-skip-permissions path. The log
# shows denials in -p mode are instant; the hangs were auth and network.
CLAUDE_FLAGS=(
  --settings "$SETTINGS"
  --setting-sources user,project
  --strict-mcp-config --mcp-config .mcp.json
  --max-turns "$MAX_TURNS"
  --permission-mode "$PERMISSION_MODE"
)
ver_num="$(printf '%s' "$CLAUDE_VER" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1)"
if [ -n "$ver_num" ]; then
  IFS=. read -r v_major v_minor v_patch <<< "$ver_num"
  if [ "$v_major" -gt 2 ] || { [ "$v_major" -eq 2 ] && { [ "$v_minor" -gt 1 ] || { [ "$v_minor" -eq 1 ] && [ "$v_patch" -ge 259 ]; }; }; }; then
    CLAUDE_FLAGS+=(--permission-prompts none)
  fi
fi

# --- run with a hard timeout -------------------------------------------------
# macOS has no `timeout`; a background watchdog kills the run instead. Runs
# lasted up to three hours before this. TERM first (claude exits 143 and
# runs SessionEnd hooks), KILL fifteen seconds later if it is still there.
claude -p "$PROMPT" "${CLAUDE_FLAGS[@]}" >> "$LOG" 2>&1 &
CLAUDE_PID=$!
(
  trap 'kill "$SLEEP_PID" 2>/dev/null; exit 0' TERM
  sleep "$TIMEOUT_SECS" & SLEEP_PID=$!
  wait "$SLEEP_PID"
  if kill -0 "$CLAUDE_PID" 2>/dev/null; then
    printf '%s  TIMEOUT: run exceeded %ss — killing claude (pid %s)\n' \
      "$(date '+%Y-%m-%d %H:%M:%S')" "$TIMEOUT_SECS" "$CLAUDE_PID" >> "$LOG"
    kill -TERM "$CLAUDE_PID" 2>/dev/null
    sleep 15
    kill -KILL "$CLAUDE_PID" 2>/dev/null
  fi
) &
WATCHDOG_PID=$!
wait "$CLAUDE_PID"
STATUS=$?
kill -TERM "$WATCHDOG_PID" 2>/dev/null
wait "$WATCHDOG_PID" 2>/dev/null

printf '\n' >> "$LOG"
if [ $STATUS -eq 0 ]; then
  log "=== $MODE run finished ok ==="
  hc_ping ok
else
  log "=== $MODE run FAILED (exit $STATUS) — check the output above ==="
  hc_ping fail
  STATUS=1
fi
exit $STATUS
