"""A silent scanner must not read as a calm market.

`alerts` answers "is there standing macro risk?" by looking for alerts in a
window. If the Analyst stops running, that query keeps returning CLEAR — not
because there is no risk, but because nothing is looking for any.

This is not hypothetical. The Analyst's thread wedged on a hung RSS feed for
56 hours, and for every one of those hours the gate answered CLEAR. An
unattended run would have taken that as permission to trade.

Same distinction the broker snapshot draws between `confirmed_empty` and
`blind`, applied to the risk gate.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import tracking

from test_tracking import TrackingTestCase


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestAgentLastActive(TrackingTestCase):
    def test_returns_none_when_agent_never_ran(self):
        self.assertIsNone(tracking.agent_last_active("analyst"))

    def test_returns_most_recent_event_ts(self):
        tracking.log_event("news_scan", source="analyst", ts=_ago(5))
        tracking.log_event("news_scan", source="analyst", ts=_ago(1))
        last = tracking.agent_last_active("analyst")
        self.assertIsNotNone(last)
        age_h = (datetime.now(timezone.utc)
                 - datetime.strptime(last, "%Y-%m-%dT%H:%M:%SZ")
                 .replace(tzinfo=timezone.utc)).total_seconds() / 3600
        self.assertLess(age_h, 2, "should report the newest scan, not the oldest")

    def test_scoped_to_the_named_agent(self):
        tracking.log_event("risk_scan", source="risk_manager", ts=_ago(1))
        self.assertIsNone(tracking.agent_last_active("analyst"),
                          "another agent's activity is not the analyst's")


class TestAlertsGateFailsClosed(unittest.TestCase):
    """End-to-end through the CLI, because the exit code is what binds."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        self._old = tracking.DB_PATH
        tracking.DB_PATH = self.db
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old
        self._tmp.cleanup()

    def _run(self, *extra):
        """Invoke the real CLI in a subprocess so we test the true exit code."""
        env_db = str(self.db)
        code = (
            "import sys; from pathlib import Path;"
            "from app import tracking;"
            f"tracking.DB_PATH = Path({env_db!r});"
            "from app.agent_cli import main;"
            f"sys.argv = ['app', 'alerts', '--agent', 'trader', '--hours', '12', "
            f"'--from', 'analyst', {', '.join(repr(e) for e in extra)}];"
            "sys.exit(main())"
        ) if extra else (
            "import sys; from pathlib import Path;"
            "from app import tracking;"
            f"tracking.DB_PATH = Path({env_db!r});"
            "from app.agent_cli import main;"
            "sys.argv = ['app', 'alerts', '--agent', 'trader', '--hours', '12',"
            " '--from', 'analyst'];"
            "sys.exit(main())"
        )
        return subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True,
                              cwd=str(Path(__file__).resolve().parent.parent))

    def test_stale_analyst_stands_down_even_with_no_alerts(self):
        """The exact 56-hour failure, pinned."""
        tracking.log_event("news_scan", source="analyst", ts=_ago(56))
        r = self._run()
        self.assertEqual(r.returncode, 1, f"expected stand-down\n{r.stdout}{r.stderr}")
        self.assertIn("STALE", r.stdout)

    def test_never_ran_stands_down(self):
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("STALE", r.stdout)

    def test_fresh_analyst_with_no_alerts_is_clear(self):
        tracking.log_event("news_scan", source="analyst", ts=_ago(0.2))
        r = self._run()
        self.assertEqual(r.returncode, 0, f"expected clear\n{r.stdout}{r.stderr}")
        self.assertIn("CLEAR", r.stdout)

    def test_fresh_analyst_with_an_alert_stands_down(self):
        tracking.log_event("news_scan", source="analyst", ts=_ago(0.2))
        tracking.send_message("analyst", "trader", "Macro risk in headlines: fed/rates",
                              priority="high", kind="alert")
        r = self._run()
        self.assertEqual(r.returncode, 1)
        self.assertIn("STANDING RISK", r.stdout)

    def test_staleness_limit_is_tunable(self):
        tracking.log_event("news_scan", source="analyst", ts=_ago(5))
        self.assertEqual(self._run().returncode, 1, "5h stale under the 2h default")
        r = self._run("--max-staleness-hours", "8")
        self.assertEqual(r.returncode, 0, "explicitly tolerated, so clear")


if __name__ == "__main__":
    unittest.main()
