#!/usr/bin/env bash
# Step 2 of 2. Run `claude setup-token` first in Terminal and finish its
# prompts; it prints a token starting sk-ant-oat. Then run this and paste the
# token when asked — nothing is echoed to the screen, and it lands only in
# ~/.config/trade/env (mode 600).
set -euo pipefail
ENV_FILE="$HOME/.config/trade/env"
mkdir -p "$(dirname "$ENV_FILE")"; chmod 700 "$(dirname "$ENV_FILE")"
printf 'Paste the token, then press Return TWICE (nothing will be shown): '
TOKEN=""
while IFS= read -rs LINE; do
  [ -z "$LINE" ] && break
  TOKEN="$TOKEN$LINE"
done; echo
TOKEN="${TOKEN//[[:space:]]/}"
case "$TOKEN" in sk-ant-oat*) ;; *) echo "That doesn't look like a setup token (expected it to start with sk-ant-oat). Nothing saved." >&2; exit 1;; esac
{ grep -v '^export CLAUDE_CODE_OAUTH_TOKEN=' "$ENV_FILE" 2>/dev/null || true; echo "export CLAUDE_CODE_OAUTH_TOKEN=$TOKEN"; } > "$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
echo "Saved to $ENV_FILE. Done."
