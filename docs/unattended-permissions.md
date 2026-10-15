# Unattended runs: permissions, credentials, and the morning checklist

This is the operations manual for `autotrade.sh` — the scheduled run that
executes the routine in `CLAUDE.md` § "Autonomous (unattended) trading"
with nobody watching. It exists because the first two months of real runs
(Aug–Oct 2026) failed for operational reasons far more often than for
trading ones: an expired login killed 22 consecutive runs, the Mac slept
mid-order three times, a stale lock could have silently skipped every
future day, and the interactive allowlist had grown to include
`Bash(python3 *)` and `Bash(security dump-keychain *)`.

Everything below was checked against the Claude Code docs on 2026-10-11:
[CLI reference](https://code.claude.com/docs/en/cli-reference),
[headless mode](https://code.claude.com/docs/en/headless),
[authentication](https://code.claude.com/docs/en/authentication),
[permissions](https://code.claude.com/docs/en/permissions),
[settings precedence](https://code.claude.com/docs/en/settings).

## 1. One-time setup on the Mac

### 1a. A credential that lasts a year

The claude.ai login used interactively refreshes itself only while you use
it, and the scheduled run cannot re-login. The log shows exactly what that
looks like: `Failed to authenticate: OAuth session expired and could not be
refreshed`, every weekday, until a human notices.

`claude setup-token` mints a one-year OAuth token tied to your subscription
([docs](https://code.claude.com/docs/en/authentication#generate-a-long-lived-token)).
It prints the token and saves nothing, so store it in a file that is
outside the repo and readable only by you:

```sh
claude setup-token                       # opens the browser; approve; copy the token
mkdir -p ~/.config/trade
echo 'export CLAUDE_CODE_OAUTH_TOKEN=<paste-token-here>' > ~/.config/trade/env
chmod 600 ~/.config/trade/env
```

`autotrade.sh` and `scripts/preflight.sh` both `source` that file if it
exists. The same file is the place for the two optional settings:

```sh
export ANTHROPIC_API_KEY=sk-ant-...          # alternative to the OAuth token (API billing)
export HEALTHCHECK_PING_URL=https://hc-ping.com/<uuid>   # see §4
```

Precedence if both are set: `ANTHROPIC_API_KEY` wins over
`CLAUDE_CODE_OAUTH_TOKEN` ([docs](https://code.claude.com/docs/en/authentication#authentication-precedence)).
Pick one. Put a reminder in your calendar for 11 months out: the token
expires and the preflight will start reporting `AUTH_FAILED`.

Never put a token in the repo, in `.claude/`, in the plist, or in a
shell-history-visible command line. The env file is the only place.

### 1b. Trust the folder once

Allow rules in a repository's `.claude/settings.json` only apply after the
workspace trust dialog has been accepted; a `-p` run never shows the
dialog ([docs](https://code.claude.com/docs/en/permissions#project-allow-rules-and-workspace-trust)).
The run passes its rules with `--settings`, which is a command-line source
rather than a repository file, but to remove any doubt do this once:

```sh
cd ~/trade && claude      # accept the trust dialog, then exit
```

or set `projects["/Users/<you>/trade"].hasTrustDialogAccepted: true` in
`~/.claude.json`.

### 1c. Log the Robinhood MCP server in

```sh
cd ~/trade && claude mcp login robinhood-trading
```

The browser login persists into headless runs (confirmed 2026-08-06) but
does expire. The preflight (§3) tells you when.

### 1d. Keep the Mac awake and on Central time

- `autotrade.sh` wraps itself in `caffeinate -ims`, which stops idle and
  system sleep **while on AC power**. A closed lid on battery still sleeps.
  Keep it plugged in overnight, or with the lid closed on AC run
  `sudo pmset -c disablesleep 1` (and `0` to undo).
- launchd fires `StartCalendarInterval` in the Mac's local zone. If the
  Mac is on Japan time, "9:00" is 6 PM Central the previous day. The
  script refuses to run (exit 3, logged `REFUSED`) unless `date +%Z` is
  `CDT`/`CST` and the Central clock is between 08:35 and 15:30. **When
  travelling, turn off System Settings → General → Date & Time → "Set time
  zone automatically" and leave the zone on Chicago.** Everything else on
  the Mac will show Chicago time; that is the price of a scheduler that
  only knows local time.
- launchd does not run a job it missed while the Mac was asleep. That is
  what the healthcheck (§4) is for.

## 2. The permissions file

`settings.unattended.json` is committed and reviewed in git. It is
passed with `--settings`, so it applies for that session only and merges
with your other settings files; crucially, **a `deny` rule from any source
beats an `allow` from any other source** — "Rules are evaluated in order:
deny, then ask, then allow. The first match in that order determines the
outcome, and rule specificity doesn't change the order"
([docs](https://code.claude.com/docs/en/permissions)). So the deny list
holds even if `~/.claude/settings.json` allows something broader.

`--setting-sources user,project` drops `.claude/settings.local.json`
entirely for the run. That file is where every "Yes, and don't ask again"
answer lands, and the audit found it contained `Bash(python3 *)`,
`Bash(python3 -c ' *)`, `Bash(security dump-keychain *)`, `Bash(git pull *)`,
`Bash(git merge *)`, `Bash(launchctl *)`, `Bash(open *)`,
`Bash(./install-autotrade.sh live *)`, `WebSearch`, a dozen dead one-off
entries with scratchpad paths, and allowed order placement without
`review_equity_order`. It is still fine for interactive use; it just never
reaches the unattended run any more.

### What the run may do

| Allow entry | Why |
| --- | --- |
| `Bash(python3 -m app evaluate *)`, `report`, `alerts`, `inbox`, `scan`, `analyze`, `decisions`, `position-scan`, `risk-scan`, `pattern-scan` | the routine's read/analyze steps |
| `Bash(python3 -m app log *)`, `ingest *` | the audit trail: proposals, orders, fills, notes, snapshots |
| `Bash(python3 -m app autonomy status *)`, `check *`, `reserve *`, `record *`, `release *`, `reconcile *` | read the policy, gate an order, and record a placed one; never change the policy |
| `Bash(echo *)` | so `echo '<json>' \| python3 -m app ingest --payload -` works — pipes are split on `\|` and each side needs a rule |
| `Read(./**)`, `Grep`, `Glob` | read the repo (CLAUDE.md, app/, journal exports). Not `Read` bare: that would include `~/.config/trade/env` |
| `mcp__robinhood-trading__get_accounts`, `get_portfolio`, `get_equity_positions`, `get_option_positions`, `get_equity_orders`, `get_option_orders`, `get_equity_quotes`, `get_option_quotes`, `get_option_chains`, `get_option_instruments`, `get_earnings_calendar` | read-only broker calls the routine needs |
| `mcp__robinhood-trading__review_equity_order`, `place_equity_order`, `review_option_order`, `place_option_order` | the four order tools, and only after `autonomy check` says yes. Dry mode denies them in the prompt, not in this file, so the file is identical in both modes |

A trailing ` *` also matches the bare command (`Bash(ls *)` matches `ls`),
so one entry per subcommand is enough.

### What the run may never do

| Deny entry | Why |
| --- | --- |
| `Bash(python3 -m app autonomy set*)`, `enable*`, `disable*` | the policy is the owner's standing instruction; `autonomy.json` is also `chmod 444` for the run |
| `Bash(python3 -c*)`, `Bash(python *)` | arbitrary code; also catches `python -m app` which fails on this Mac anyway |
| `Bash(security *)` | keychain access — the old allowlist had `security dump-keychain` |
| `Bash(git *)`, `Bash(launchctl *)`, `Bash(open *)`, `Bash(./install-autotrade.sh*)`, `Bash(pmset *)`, `Bash(sudo *)`, `Bash(chmod *)` | the run must not change the code, the schedule, its own mode, power settings, or undo the read-only policy file |
| `Bash(curl *)`, `WebSearch`, `WebFetch` | no exfiltration path and no unvetted data source; news comes through the Analyst |
| `mcp__robinhood-trading__cancel_*`, `exercise_option`, `place_crypto_order`, `preview_crypto_order` | cancelling is the owner's job (CLAUDE.md step 6); crypto and exercise are out of scope |
| `Read(~/.config/trade/**)`, `Read(~/.claude/.credentials.json)` | the token must never land in `autotrade.log` |
| `Edit`, `Write`, `NotebookEdit` | the run writes through the CLI into `journal.db`, nothing else. A bare tool name in deny removes the tool from the model's context entirely |

Anything not listed is denied when it would prompt: the run passes
`--permission-mode dontAsk` ("Claude Code denies every call that would
otherwise prompt, which is useful for locked-down CI runs" —
[docs](https://code.claude.com/docs/en/headless#auto-approve-tools)) and,
on Claude Code ≥ 2.1.259, `--permission-prompts none`, which also tells the
model not to retry a denied call
([docs](https://code.claude.com/docs/en/headless#turn-off-permission-prompts-in-unattended-runs)).

Note for `CLAUDE.md` step 7: it currently shows a Python snippet
(`from app import autonomy; autonomy.record_autonomous_order(...)`). With
`python3 -c` denied the run cannot execute that literally; it records the
order through `python3 -m app autonomy record ...` / `log order ...` and
the `autonomy reserve/release/reconcile` subcommands instead.

### The full `claude` command the run executes

```
claude -p "<routine prompt>" \
  --settings settings.unattended.json \
  --setting-sources user,project \
  --strict-mcp-config --mcp-config .mcp.json \
  --max-turns 60 \
  --permission-mode dontAsk \
  --permission-prompts none        # only when claude --version >= 2.1.259
```

- `--strict-mcp-config --mcp-config .mcp.json`: "Only use MCP servers from
  `--mcp-config`, ignoring all other MCP configurations" — no claude.ai
  connectors, nothing from `~/.claude.json`.
- `--max-turns 60`: "Exits with an error when the limit is reached. No limit
  by default." Override with `AUTOTRADE_MAX_TURNS`.
- There is no `--dangerously-skip-permissions` escape hatch any more. The
  log shows permission denials in `-p` mode are instant and reported; the
  hangs were auth and network.
- `--bare` is deliberately **not** used: it skips `CLAUDE.md` (the routine
  lives there) and "does not read `CLAUDE_CODE_OAUTH_TOKEN`".

## 3. The 08:30 preflight

`scripts/preflight.sh` answers "will 9 AM work?" before 9 AM. It runs
`claude mcp list` (no model call; prints `✔ Connected` / `! Needs
authentication` per server) and then `claude -p "reply ok" --max-turns 1`
under a 2-minute cap, and writes one line to `preflight.status` and to
`autotrade.log`:

```
2026-10-12 OK model answered; mcp: robinhood-trading: https://agent.robinhood.com/mcp/trading (HTTP) - ✔ Connected
2026-10-12 AUTH_FAILED Failed to authenticate ... — run: claude setup-token, then update ~/.config/trade/env
2026-10-12 MCP_NEEDS_AUTH ... — run: claude mcp login robinhood-trading
2026-10-12 NETWORK model call timed out after 120s; ...
```

`autotrade.sh` reads today's line. On `AUTH_FAILED` or `MCP_NEEDS_AUTH` a
`live` run is downgraded to `dry` with a `DOWNGRADED live -> dry` log line,
because a run that can log a proposal but cannot see the broker is worse
than a run that only reports. A missing or stale line is logged as `INFO`
and the run proceeds as scheduled: the preflight is a monitor, not a lock.

It is not installed by `install-autotrade.sh`. To install it, save this as
`~/Library/LaunchAgents/com.iannxavierr.trade.preflight.plist` (replace
`/Users/you/trade`):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.iannxavierr.trade.preflight</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>/Users/you/trade/scripts/preflight.sh</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/you/trade</string>
  <key>StartCalendarInterval</key>
  <array>
    <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
    <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>8</integer><key>Minute</key><integer>30</integer></dict>
  </array>
  <key>StandardOutPath</key><string>/Users/you/trade/launchd-stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/you/trade/launchd-stderr.log</string>
</dict>
</plist>
```

then:

```sh
plutil -lint ~/Library/LaunchAgents/com.iannxavierr.trade.preflight.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.iannxavierr.trade.preflight.plist
launchctl list | grep trade        # both jobs should be listed
```

The cron equivalent, for a Linux box:

```
30 8 * * 1-5 cd /home/you/trade && bash scripts/preflight.sh >> cron-stderr.log 2>&1
```

For the 9 AM job itself, `./install-autotrade.sh --launchd live` writes and
loads `com.iannxavierr.trade.autotrade.plist` with the same shape.

## 4. Knowing when it did not run

A dead Mac, an expired token, and a holiday all look the same from the
outside: no log line. The fix is a dead-man's switch: make a free check at
[healthchecks.io](https://healthchecks.io) with a weekday 09:00 CT schedule
and a one-hour grace period, and put its URL in the env file as
`HEALTHCHECK_PING_URL`. Both scripts then ping `<url>/start` when they
begin, `<url>` on success and `<url>/fail` on failure, and the service
emails you when a ping is late or a `/fail` arrives. Holiday and weekend
skips ping success (a planned no-op is healthy); `REFUSED`, `SKIPPED`,
timeouts and auth failures ping `/fail`.

## 5. Reading the log

Every run now starts with a header:

```
2026-10-12 09:00:03  === starting live run ===
2026-10-12 09:00:03  commit=1a2b3c4 claude='2.1.270 (Claude Code)' tz=CDT central=2026-10-12 0900 mode=live max_turns=60 timeout=2700s
2026-10-12 09:00:03  autonomy: Autonomous trading: ENABLED per-trade cap: $300.00 daily trade cap: 1 (used today: 0) ...
```

Other lines to know:

| Line | Meaning | Exit |
| --- | --- | --- |
| `SKIPPED (market closed): ...` | weekend or NYSE holiday from `scripts/nyse_holidays.py` (2026–2027 listed; add each December) | 0 |
| `REFUSED: Mac time zone is 'JST'` / `outside the 08:35-15:30 run window` | clock guard | 3 |
| `SKIPPED: a previous run (pid N) is still going` | lock held by a live process | 2 |
| `WARN: stale lock from pid 'N' (not running) — removing it` | a crashed run left `.autotrade.lock`; recovered automatically | — |
| `DOWNGRADED live -> dry: preflight reported AUTH_FAILED` | §3 | — |
| `TIMEOUT: run exceeded 2700s — killing claude` | the 45-minute watchdog (`AUTOTRADE_TIMEOUT_SECS`) | 1 |
| `=== live run FAILED (exit N)` | claude returned non-zero; the reason is in the lines above | 1 |

## 6. Files that contain account numbers

`journal.db`, `autotrade.log`, `preflight.status` and
`.claude/settings.local.json` all carry real account numbers, balances and
position sizes (the CLI quotes broker payloads back verbatim, and the old
allowlist contained full `log note` command strings with them). They are
gitignored and must stay that way; also keep `~/trade` out of iCloud
Drive, Dropbox and any other synced folder — Desktop/Documents sync by
default on macOS, which is one more reason the repo lives at `~/trade`.
`settings.unattended.json`, by contrast, contains only rule
patterns and is safe to commit.

## 7. Testing without a Mac or a token

`tests/test_autotrade_sh.py` runs `autotrade.sh` against a stub `claude`
on `PATH` with `AUTOTRADE_NOW="2026-10-12 09:00 CDT"` faking the clock,
and checks the lock, the stale-lock recovery, the exit codes, the holiday
skip, the header lines and the preflight downgrade. `python3 -m unittest
discover -s tests` picks it up; it skips itself where `bash` is missing.
