"""Tests for the unattended-run plumbing: autotrade.sh, scripts/preflight.sh,
scripts/nyse_holidays.py and install-autotrade.sh --launchd.

The shell scripts are exercised for real, inside a temp copy of the repo,
with a stub `claude` first on PATH and the clock faked through
AUTOTRADE_NOW. No network, no model, no token. Skips itself where bash is
missing (Windows), so `python3 -m unittest discover -s tests` stays green
everywhere.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASH = shutil.which("bash")

# Stub `claude`. Records argv, answers --version / mcp list, and behaves per
# STUB_CLAUDE_BEHAVIOR so tests can force an auth failure or a hang.
STUB_CLAUDE = textwrap.dedent(r"""
    #!/usr/bin/env bash
    if [ "${1:-}" = "--version" ]; then
      echo "${STUB_CLAUDE_VERSION:-2.1.270 (Claude Code)}"; exit 0
    fi
    if [ "${1:-}" = "mcp" ]; then
      echo "robinhood-trading: https://agent.robinhood.com/mcp/trading (HTTP) - ${STUB_MCP_STATUS:-✔ Connected}"
      exit 0
    fi
    [ -n "${STUB_ARGS_FILE:-}" ] && printf '%s\x1f' "$@" > "$STUB_ARGS_FILE"
    if [ -n "${STUB_POLICY_MODE_FILE:-}" ] && [ -f autonomy.json ]; then
      python3 -c 'import os;print(oct(os.stat("autonomy.json").st_mode)[-3:])' > "$STUB_POLICY_MODE_FILE"
    fi
    case "${STUB_CLAUDE_BEHAVIOR:-ok}" in
      ok)   echo "stub run complete"; exit 0 ;;
      auth) echo "Failed to authenticate: OAuth session expired and could not be refreshed"; exit 1 ;;
      fail) echo "boom"; exit 1 ;;
      hang) sleep 60; exit 0 ;;
    esac
""").lstrip()

# Stub `app` package so `python3 -m app autonomy status` works in the temp
# repo without importing the real application (and its journal.db).
STUB_APP_MAIN = textwrap.dedent("""
    import sys
    if sys.argv[1:3] == ["autonomy", "status"]:
        print("Autonomous trading: DISABLED (stub)")
        print("  per-trade cap:      $300.00")
    else:
        print("stub app:", " ".join(sys.argv[1:]))
""").lstrip()


@unittest.skipIf(BASH is None, "bash not available")
class AutotradeShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="autotrade-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        (self.repo / "scripts").mkdir(parents=True)
        (self.repo / ".claude").mkdir()
        (self.repo / "app").mkdir()
        for rel in ("autotrade.sh", "install-autotrade.sh", "scripts/preflight.sh",
                    "scripts/nyse_holidays.py", "settings.unattended.json",
                    ".mcp.json"):
            shutil.copy(REPO / rel, self.repo / rel)
        (self.repo / "app" / "__init__.py").write_text("")
        (self.repo / "app" / "__main__.py").write_text(STUB_APP_MAIN)

        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        stub = self.bin / "claude"
        stub.write_text(STUB_CLAUDE)
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
        self.args_file = self.tmp / "claude-args.txt"
        self.home = self.tmp / "home"
        self.home.mkdir()

    # -- helpers ----------------------------------------------------------
    def env(self, **extra):
        e = {
            "PATH": f"{self.bin}:{os.environ.get('PATH', '')}",
            "HOME": str(self.home),
            "TZ": "America/Chicago",
            "AUTOTRADE_NOW": "2026-10-12 09:00 CDT",   # a Monday, trading day
            "AUTOTRADE_ENV_FILE": str(self.tmp / "no-such-env-file"),
            "STUB_ARGS_FILE": str(self.args_file),
            "LANG": "C.UTF-8",
        }
        e.update({k: str(v) for k, v in extra.items()})
        return e

    def run_autotrade(self, mode="dry", timeout=60, **extra):
        return subprocess.run(
            [BASH, "autotrade.sh", mode], cwd=self.repo, env=self.env(**extra),
            capture_output=True, text=True, timeout=timeout)

    def log(self):
        p = self.repo / "autotrade.log"
        return p.read_text() if p.exists() else ""

    def claude_args(self):
        # the stub joins argv with \x1f because the prompt spans lines
        if not self.args_file.exists():
            return []
        return self.args_file.read_text().rstrip("\x1f").split("\x1f")

    # -- syntax ----------------------------------------------------------
    def test_scripts_parse(self):
        for rel in ("autotrade.sh", "install-autotrade.sh", "scripts/preflight.sh"):
            r = subprocess.run([BASH, "-n", str(REPO / rel)], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, f"{rel}: {r.stderr}")

    def test_no_skip_permissions_escape_hatch(self):
        text = (REPO / "autotrade.sh").read_text()
        self.assertNotIn("skip-permissions\"", text.replace("--dangerously-skip-permissions path", ""))
        self.assertNotIn("a hang means a permission prompt", text.lower())

    # -- happy path --------------------------------------------------------
    def test_dry_run_header_flags_and_lock(self):
        r = self.run_autotrade("dry")
        self.assertEqual(r.returncode, 0, r.stderr + self.log())
        log = self.log()
        self.assertIn("=== starting dry run ===", log)
        self.assertIn("commit=", log)
        self.assertIn("claude='2.1.270 (Claude Code)'", log)
        self.assertIn("tz=CDT", log)
        self.assertIn("mode=dry", log)
        self.assertIn("autonomy: Autonomous trading: DISABLED (stub) per-trade cap:", log)
        self.assertIn("=== dry run finished ok ===", log)
        self.assertFalse((self.repo / ".autotrade.lock").exists(), "lock not released")

        args = self.claude_args()
        self.assertEqual(args[0], "-p")
        self.assertIn("DRY RUN", args[1])
        for flag in ("--settings", "--strict-mcp-config", "--mcp-config",
                     "--max-turns", "--permission-mode", "--setting-sources",
                     "--permission-prompts"):
            self.assertIn(flag, args, f"missing {flag}")
        self.assertEqual(args[args.index("--settings") + 1], "settings.unattended.json")
        self.assertEqual(args[args.index("--max-turns") + 1], "60")
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--setting-sources") + 1], "user,project")
        self.assertEqual(args[args.index("--mcp-config") + 1], ".mcp.json")
        self.assertNotIn("--dangerously-skip-permissions", args)

    def test_live_prompt_and_max_turns_override(self):
        r = self.run_autotrade("live", AUTOTRADE_MAX_TURNS=7)
        self.assertEqual(r.returncode, 0)
        args = self.claude_args()
        self.assertNotIn("DRY RUN", args[1])
        self.assertEqual(args[args.index("--max-turns") + 1], "7")
        self.assertIn("=== starting live run ===", self.log())

    def test_permission_prompts_flag_skipped_on_old_claude(self):
        self.run_autotrade("dry", STUB_CLAUDE_VERSION="2.1.100 (Claude Code)")
        self.assertNotIn("--permission-prompts", self.claude_args())

    def test_env_file_is_sourced(self):
        env_file = self.tmp / "env"
        env_file.write_text("export AUTOTRADE_MAX_TURNS=11\n")
        self.run_autotrade("dry", AUTOTRADE_ENV_FILE=env_file)
        args = self.claude_args()
        self.assertEqual(args[args.index("--max-turns") + 1], "11")

    def test_policy_file_read_only_during_run_and_restored(self):
        policy = self.repo / "autonomy.json"
        policy.write_text("{}")
        policy.chmod(0o644)
        mode_file = self.tmp / "mode-during-run"
        self.run_autotrade("dry", STUB_POLICY_MODE_FILE=mode_file)
        self.assertEqual(mode_file.read_text().strip(), "444")
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o644)

    # -- failure paths -----------------------------------------------------
    def test_bad_mode_exits_2(self):
        r = self.run_autotrade("--help")
        self.assertEqual(r.returncode, 2)
        self.assertIn("unknown mode", self.log())

    def test_claude_failure_exits_1(self):
        r = self.run_autotrade("dry", STUB_CLAUDE_BEHAVIOR="fail")
        self.assertEqual(r.returncode, 1)
        self.assertIn("run FAILED", self.log())
        self.assertFalse((self.repo / ".autotrade.lock").exists())

    def test_timeout_kills_run(self):
        t0 = time.time()
        r = self.run_autotrade("dry", STUB_CLAUDE_BEHAVIOR="hang", AUTOTRADE_TIMEOUT_SECS=2, timeout=90)
        self.assertEqual(r.returncode, 1)
        self.assertLess(time.time() - t0, 40, "watchdog did not fire")
        self.assertIn("TIMEOUT: run exceeded 2s", self.log())
        self.assertFalse((self.repo / ".autotrade.lock").exists())

    # -- lock ----------------------------------------------------------------
    def test_live_lock_skips_with_exit_2(self):
        lock = self.repo / ".autotrade.lock"
        lock.mkdir()
        holder = subprocess.Popen(["sleep", "30"])
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        (lock / "pid").write_text(str(holder.pid))
        r = self.run_autotrade("dry")
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"SKIPPED: a previous run (pid {holder.pid}) is still going", self.log())
        self.assertTrue(lock.exists(), "a live lock must not be removed")
        self.assertEqual(self.claude_args(), [])

    def test_stale_lock_is_recovered(self):
        lock = self.repo / ".autotrade.lock"
        lock.mkdir()
        (lock / "pid").write_text("999999999")
        r = self.run_autotrade("dry")
        self.assertEqual(r.returncode, 0, self.log())
        self.assertIn("WARN: stale lock from pid '999999999'", self.log())
        self.assertIn("=== dry run finished ok ===", self.log())
        self.assertFalse(lock.exists())

    def test_stale_lock_without_pid_file_is_recovered(self):
        (self.repo / ".autotrade.lock").mkdir()   # the pre-fix lock shape
        r = self.run_autotrade("dry")
        self.assertEqual(r.returncode, 0, self.log())
        self.assertIn("WARN: stale lock", self.log())

    # -- clock guards ----------------------------------------------------------
    def test_wrong_timezone_exits_3(self):
        r = self.run_autotrade("live", AUTOTRADE_NOW="2026-10-12 09:00 JST")
        self.assertEqual(r.returncode, 3)
        self.assertIn("REFUSED: Mac time zone is 'JST'", self.log())
        self.assertEqual(self.claude_args(), [])

    def test_outside_window_exits_3(self):
        for when in ("2026-10-12 08:34 CDT", "2026-10-12 15:31 CDT", "2026-10-12 23:00 CST"):
            self.args_file.unlink(missing_ok=True)
            r = self.run_autotrade("live", AUTOTRADE_NOW=when)
            self.assertEqual(r.returncode, 3, when)
            self.assertEqual(self.claude_args(), [], when)
        self.assertIn("outside the 08:35-15:30 run window", self.log())

    def test_window_edges_run(self):
        for when in ("2026-10-12 08:35 CDT", "2026-10-12 15:30 CST"):
            r = self.run_autotrade("dry", AUTOTRADE_NOW=when)
            self.assertEqual(r.returncode, 0, when)

    # -- holidays ----------------------------------------------------------------
    def test_holiday_skips_with_exit_0(self):
        r = self.run_autotrade("live", AUTOTRADE_NOW="2026-11-26 09:00 CST")
        self.assertEqual(r.returncode, 0)
        self.assertIn("SKIPPED (market closed): 2026-11-26 is Thanksgiving Day", self.log())
        self.assertEqual(self.claude_args(), [])
        self.assertFalse((self.repo / ".autotrade.lock").exists())

    def test_weekend_skips(self):
        r = self.run_autotrade("live", AUTOTRADE_NOW="2026-10-10 09:00 CDT")
        self.assertEqual(r.returncode, 0)
        self.assertIn("is a Saturday", self.log())

    def test_unknown_year_warns_and_runs(self):
        r = self.run_autotrade("dry", AUTOTRADE_NOW="2031-03-03 09:00 CST")
        self.assertEqual(r.returncode, 0)
        self.assertIn("WARN: holiday check inconclusive", self.log())
        self.assertIn("=== starting dry run ===", self.log())

    # -- preflight coupling ----------------------------------------------------
    def test_preflight_auth_failed_downgrades_live_to_dry(self):
        (self.repo / "preflight.status").write_text("2026-10-12 AUTH_FAILED token expired\n")
        r = self.run_autotrade("live")
        self.assertEqual(r.returncode, 0)
        self.assertIn("DOWNGRADED live -> dry: preflight reported AUTH_FAILED", self.log())
        self.assertIn("=== starting dry run ===", self.log())
        self.assertIn("DRY RUN", self.claude_args()[1])

    def test_preflight_mcp_needs_auth_downgrades(self):
        (self.repo / "preflight.status").write_text("2026-10-12 MCP_NEEDS_AUTH login\n")
        self.run_autotrade("live")
        self.assertIn("DOWNGRADED live -> dry", self.log())

    def test_stale_or_ok_preflight_keeps_live(self):
        (self.repo / "preflight.status").write_text("2026-10-09 AUTH_FAILED old\n")
        self.run_autotrade("live")
        self.assertIn("INFO: no preflight result for 2026-10-12", self.log())
        self.assertNotIn("DRY RUN", self.claude_args()[1])

        (self.repo / "preflight.status").write_text("2026-10-12 OK fine\n")
        self.run_autotrade("live")
        self.assertNotIn("DOWNGRADED", self.log())
        self.assertNotIn("DRY RUN", self.claude_args()[1])

    # -- preflight.sh itself -----------------------------------------------------
    def run_preflight(self, **extra):
        return subprocess.run([BASH, "scripts/preflight.sh"], cwd=self.repo,
                              env=self.env(**extra), capture_output=True, text=True, timeout=60)

    def test_preflight_ok(self):
        r = self.run_preflight()
        self.assertEqual(r.returncode, 0, r.stderr + self.log())
        status = (self.repo / "preflight.status").read_text()
        self.assertTrue(status.startswith("2026-10-12 OK "), status)
        self.assertIn("Connected", status)
        self.assertIn("PREFLIGHT OK", self.log())
        args = self.claude_args()
        self.assertEqual(args[:2], ["-p", "reply ok"])
        self.assertEqual(args[args.index("--max-turns") + 1], "1")

    def test_preflight_auth_failed(self):
        r = self.run_preflight(STUB_CLAUDE_BEHAVIOR="auth")
        self.assertEqual(r.returncode, 1)
        status = (self.repo / "preflight.status").read_text()
        self.assertTrue(status.startswith("2026-10-12 AUTH_FAILED "), status)
        self.assertIn("claude setup-token", status)

    def test_preflight_mcp_needs_auth(self):
        r = self.run_preflight(STUB_MCP_STATUS="! Needs authentication")
        self.assertEqual(r.returncode, 1)
        status = (self.repo / "preflight.status").read_text()
        self.assertTrue(status.startswith("2026-10-12 MCP_NEEDS_AUTH "), status)
        self.assertIn("claude mcp login robinhood-trading", status)

    def test_preflight_network_timeout(self):
        r = self.run_preflight(STUB_CLAUDE_BEHAVIOR="hang", PREFLIGHT_TIMEOUT_SECS=2)
        self.assertEqual(r.returncode, 1)
        self.assertTrue((self.repo / "preflight.status").read_text().startswith("2026-10-12 NETWORK "))

    # -- install-autotrade.sh --launchd --------------------------------------------
    def test_launchd_install_show_remove(self):
        agents = self.tmp / "LaunchAgents"
        env = self.env(LAUNCH_AGENTS_DIR=agents, LAUNCHCTL_CMD="true")
        r = subprocess.run([BASH, "install-autotrade.sh", "--launchd", "live", "9:05"],
                           cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        plist = agents / "com.iannxavierr.trade.autotrade.plist"
        self.assertTrue(plist.exists())
        text = plist.read_text()
        self.assertIn("<string>live</string>", text)
        self.assertIn(f"<string>{self.repo}/autotrade.sh</string>", text)
        self.assertEqual(text.count("<key>Weekday</key>"), 5)
        self.assertIn("<key>Hour</key><integer>9</integer><key>Minute</key><integer>5</integer>", text)

        r = subprocess.run([BASH, "install-autotrade.sh", "--launchd", "--show"],
                           cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertIn("com.iannxavierr.trade.autotrade", r.stdout)

        r = subprocess.run([BASH, "install-autotrade.sh", "--launchd", "--remove"],
                           cwd=self.repo, env=env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(plist.exists())

    def test_launchd_rejects_bad_args(self):
        env = self.env(LAUNCH_AGENTS_DIR=self.tmp / "LA", LAUNCHCTL_CMD="true")
        for args in (["--launchd", "maybe"], ["--launchd", "dry", "25:00"]):
            r = subprocess.run([BASH, "install-autotrade.sh", *args],
                               cwd=self.repo, env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 2, args)


class NyseHolidayTests(unittest.TestCase):
    def setUp(self):
        sys.path.insert(0, str(REPO / "scripts"))
        self.addCleanup(sys.path.remove, str(REPO / "scripts"))
        import nyse_holidays
        self.mod = nyse_holidays

    def test_known_holidays(self):
        for d in (date(2026, 1, 1), date(2026, 4, 3), date(2026, 7, 3), date(2026, 11, 26),
                  date(2026, 12, 25), date(2027, 3, 26), date(2027, 6, 18), date(2027, 7, 5),
                  date(2027, 12, 24)):
            code, msg = self.mod.status(d)
            self.assertEqual(code, 1, msg)
            self.assertIn("NYSE closed", msg)

    def test_all_holidays_fall_on_weekdays(self):
        for d in self.mod.HOLIDAYS:
            self.assertLess(d.weekday(), 5, d)

    def test_trading_day_and_weekend_and_unknown_year(self):
        self.assertEqual(self.mod.status(date(2026, 10, 12))[0], 0)
        self.assertEqual(self.mod.status(date(2026, 11, 27))[0], 0)  # early close, still open
        self.assertEqual(self.mod.status(date(2026, 10, 11))[0], 1)  # Sunday
        self.assertEqual(self.mod.status(date(2031, 1, 2))[0], 2)

    def test_cli_exit_codes(self):
        script = str(REPO / "scripts" / "nyse_holidays.py")

        def rc(arg):
            return subprocess.run([sys.executable, script, arg], capture_output=True).returncode

        self.assertEqual(rc("2026-10-12"), 0)
        self.assertEqual(rc("2026-09-07"), 1)
        self.assertEqual(rc("2031-01-02"), 2)
        self.assertEqual(rc("nope"), 2)


if __name__ == "__main__":
    unittest.main()
