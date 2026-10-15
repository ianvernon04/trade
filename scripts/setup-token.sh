#!/usr/bin/env bash
# Run this in a real Terminal window. It runs `claude setup-token` for you
# (follow its prompts on screen), captures the token from the output, and
# writes it to ~/.config/trade/env (mode 600). No copy-paste.
set -euo pipefail
export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"
ENV_FILE="$HOME/.config/trade/env"
mkdir -p "$(dirname "$ENV_FILE")"; chmod 700 "$(dirname "$ENV_FILE")"
CAP="$(mktemp "${TMPDIR:-/tmp}/setup-token.XXXXXX")"; chmod 600 "$CAP"
trap 'rm -f "$CAP"' EXIT
echo "Running claude setup-token — follow its prompts…"
claude setup-token 2>&1 | tee "$CAP"
TOKEN="$(tr -d '\r' < "$CAP" | grep -oE 'sk-ant-oat[A-Za-z0-9_-]+' | awk '{ if (length($0) > length(best)) best=$0 } END { print best }')"
if [ -z "$TOKEN" ]; then echo "No sk-ant-oat token found in the output. Nothing saved." >&2; exit 1; fi
{ grep -v '^export CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" 2>/dev/null || true; echo "export CLAUDE_CODE_OAUTH_TOKEN=$TOKEN"; } > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
echo "Saved a ${#TOKEN}-character token to $ENV_FILE. Done."
