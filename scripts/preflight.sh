#!/usr/bin/env bash
# Morning preflight for the unattended run. Run it at 08:30 CT, half an hour
# before autotrade.sh, so a dead token or a logged-out MCP server is known
# BEFORE the trading run starts rather than discovered in its log afterwards.
#
# What it checks, cheapest first:
#   1. `claude mcp list`          — prints each MCP server's connection/auth
#                                   state; no model call, no tokens spent
#   2. `claude -p "reply ok"`     — one real model call with --max-turns 1,
#                                   2-minute cap; proves the credential works
#
# Writes ONE line to preflight.status:  "<YYYY-MM-DD> <STATUS> <detail>"
# and the same line to autotrade.log. STATUS is one of
#   OK              everything answered
#   AUTH_FAILED     claude could not authenticate (token expired/missing)
#   MCP_NEEDS_AUTH  the Robinhood server wants a browser login again
#   NETWORK         nothing answered at all (offline, DNS, proxy)
#
# autotrade.sh reads today's line and refuses to run LIVE on AUTH_FAILED or
# MCP_NEEDS_AUTH (it downgrades to dry and logs why). If
# HEALTHCHECK_PING_URL is set, the result is also pinged (/start, ok, /fail)
# so a silent Mac shows up as a missed check.
#
# Exit code: 0 on OK, 1 otherwise. Meant for launchd/cron; harmless by hand.
# The launchd plist for it is in docs/unattended-permissions.md.
#
# Env overrides (same env file as autotrade.sh):
#   AUTOTRADE_ENV_FILE         default ~/.config/trade/env
#   PREFLIGHT_TIMEOUT_SECS     cap on the model call (default 120)
#   AUTOTRADE_NOW              TESTING ONLY: "YYYY-MM-DD HH:MM ZONE" fakes the date

set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

LOG="autotrade.log"
STATUS_FILE="preflight.status"
ENV_FILE="${AUTOTRADE_ENV_FILE:-$HOME/.config/trade/env}"
MCP_SERVER="robinhood-trading"

# launchd/cron start with a near-empty PATH, so put the usual install dirs
# back. Appended, not prepended: an explicit PATH from the caller (or a test
# harness with a stub `claude`) must keep priority.
export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
export DISABLE_AUTOUPDATER=1

if [ -f "$ENV_FILE" ]; then
  # shellcheck disable=SC1090
  set -a; . "$ENV_FILE"; set +a
fi
HC_URL="${HEALTHCHECK_PING_URL:-}"
TIMEOUT_SECS="${PREFLIGHT_TIMEOUT_SECS:-120}"

log() { printf '%s  %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*" >> "$LOG"; }
hc_ping() {
  [ -n "$HC_URL" ] || return 0
  case "$1" in start) url="$HC_URL/start" ;; fail) url="$HC_URL/fail" ;; *) url="$HC_URL" ;; esac
  curl -fsS -m 10 --retry 3 -o /dev/null "$url" 2>/dev/null || log "WARN: preflight healthcheck ping ($1) failed"
}

if [ -n "${AUTOTRADE_NOW:-}" ]; then
  TODAY="${AUTOTRADE_NOW%% *}"
else
  TODAY="$(TZ=America/Chicago date +%Y-%m-%d)"
fi

# Portable timeout (macOS has none): run in background, kill on expiry.
# Prints the command's output; returns its exit code, or 124 on timeout.
run_with_timeout() {  # run_with_timeout SECS cmd args...
  local secs="$1"; shift
  local out rc
  out="$(mktemp)"
  "$@" > "$out" 2>&1 &
  local pid=$!
  # The watchdog's stdout must not be the caller's: inside $(...) an open
  # pipe would hold the substitution until the sleep ends.
  ( sleep "$secs"; kill -TERM "$pid" 2>/dev/null; sleep 5; kill -KILL "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  local wd=$!
  wait "$pid"; rc=$?
  kill "$wd" 2>/dev/null; wait "$wd" 2>/dev/null
  cat "$out"; rm -f "$out"
  # 143 = killed by our TERM; report it as 124 like coreutils timeout would
  if [ "$rc" -eq 143 ] || [ "$rc" -eq 137 ]; then return 124; fi
  return "$rc"
}

finish() {  # finish STATUS detail...
  local status="$1"; shift
  local line="$TODAY $status $*"
  printf '%s\n' "$line" > "$STATUS_FILE"
  log "PREFLIGHT $status: $*"
  if [ "$status" = "OK" ]; then hc_ping ok; exit 0; else hc_ping fail; exit 1; fi
}

hc_ping start

if ! command -v claude >/dev/null 2>&1; then
  finish AUTH_FAILED "'claude' not on PATH"
fi

# 1. MCP auth state. `claude mcp list` prints one line per server with a
#    status such as "✓ Connected" or "Needs authentication". No model call.
MCP_OUT="$(run_with_timeout 60 claude mcp list 2>&1)"
MCP_RC=$?
MCP_LINE="$(printf '%s\n' "$MCP_OUT" | grep -i -- "$MCP_SERVER" | head -n 1 | tr -s ' ' | cut -c1-160)"
if printf '%s' "$MCP_LINE" | grep -qiE 'needs auth|not authenticated|unauthenticated|authentication required'; then
  finish MCP_NEEDS_AUTH "$MCP_LINE — run: claude mcp login $MCP_SERVER"
fi
if [ "$MCP_RC" -eq 124 ]; then
  MCP_LINE="mcp list timed out"
elif [ -z "$MCP_LINE" ]; then
  MCP_LINE="server '$MCP_SERVER' not listed (mcp list exit $MCP_RC)"
fi

# 2. One cheap model call. Auth failures print to stdout in -p mode
#    ("Failed to authenticate", "OAuth ... expired"), with a non-zero exit.
MODEL_OUT="$(run_with_timeout "$TIMEOUT_SECS" claude -p "reply ok" --max-turns 1 --strict-mcp-config --mcp-config .mcp.json 2>&1)"
MODEL_RC=$?
MODEL_SNIP="$(printf '%s' "$MODEL_OUT" | tr -s ' \n' ' ' | cut -c1-200)"

if printf '%s' "$MODEL_OUT" | grep -qiE 'failed to authenticate|oauth.*(expired|invalid)|not logged in|invalid api key|authentication_error|401'; then
  finish AUTH_FAILED "$MODEL_SNIP — run: claude setup-token, then update ~/.config/trade/env"
fi
if [ "$MODEL_RC" -eq 124 ]; then
  finish NETWORK "model call timed out after ${TIMEOUT_SECS}s; mcp: $MCP_LINE"
fi
if printf '%s' "$MODEL_OUT" | grep -qiE 'ENOTFOUND|ECONNREFUSED|ECONNRESET|ETIMEDOUT|network error|fetch failed|getaddrinfo'; then
  finish NETWORK "$MODEL_SNIP"
fi
if [ "$MODEL_RC" -ne 0 ]; then
  # Unknown failure: treat as auth so a live run does not proceed on it.
  finish AUTH_FAILED "claude -p exit $MODEL_RC: $MODEL_SNIP"
fi

finish OK "model answered; mcp: ${MCP_LINE:-unknown}"
