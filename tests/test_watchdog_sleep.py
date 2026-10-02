"""The watchdog must not mistake machine sleep for a hung agent thread.

The agents mark time with `time.sleep`, which does not advance while the
machine is asleep. A laptop on battery cycles sleep/darkwake all night, so by
morning every agent reads hours stale without a thread having misbehaved. The
watchdog treated that as failure and restarted the server — in one observed
case six minutes after wake, on 491 minutes of "staleness" accumulated
entirely while the process was suspended.

Capping age at how long the machine has been awake fixes that without
disarming the watchdog: a thread that is genuinely wedged goes on being
silent as the awake clock runs, and still trips the threshold.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import watchdog
from app import tracking


def _ago(minutes: float) -> str:
    ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


class WatchdogSleepTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "test.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def _seen(self, agent: str, kind: str, minutes_ago: float):
        tracking.log_event(kind, source=agent, note="scan", ts=_ago(minutes_ago))

    def _row(self, rows, agent):
        return next(r for r in rows if r["agent"] == agent)


class TestSleepIsNotStaleness(WatchdogSleepTestCase):
    def test_overnight_sleep_does_not_look_stale(self):
        """The exact observed failure: 491m silent, 6m awake."""
        for agent, kind in (("analyst", "news_scan"),
                            ("position_manager", "position_scan"),
                            ("risk_manager", "risk_scan"),
                            ("pattern_engine", "pattern_scan")):
            self._seen(agent, kind, 491)

        rows = watchdog.check_agents(awake_min=6)
        self.assertEqual([r for r in rows if r["stale"]], [],
                         "agents silent only across a machine sleep must not "
                         "be treated as hung")
        self.assertAlmostEqual(self._row(rows, "analyst")["effective_age_min"], 6)
        self.assertAlmostEqual(self._row(rows, "analyst")["age_min"], 491, delta=1)

    def test_genuinely_hung_agent_still_trips_after_wake(self):
        """The watchdog has to keep working — silence that outlives the wake."""
        self._seen("analyst", "news_scan", 500)
        rows = watchdog.check_agents(awake_min=180)   # awake 3h, analyst threshold 60m
        self.assertTrue(self._row(rows, "analyst")["stale"])

    def test_hang_while_continuously_awake_trips(self):
        self._seen("analyst", "news_scan", 90)
        rows = watchdog.check_agents(awake_min=10_000)
        self.assertTrue(self._row(rows, "analyst")["stale"])

    def test_thresholds_still_respected_per_agent(self):
        """90m silent: past the analyst's 60m bar, inside the others'."""
        for agent, kind in (("analyst", "news_scan"),
                            ("position_manager", "position_scan"),
                            ("risk_manager", "risk_scan"),
                            ("pattern_engine", "pattern_scan")):
            self._seen(agent, kind, 90)
        rows = watchdog.check_agents(awake_min=10_000)
        self.assertTrue(self._row(rows, "analyst")["stale"])
        self.assertFalse(self._row(rows, "position_manager")["stale"])
        self.assertFalse(self._row(rows, "risk_manager")["stale"])
        self.assertFalse(self._row(rows, "pattern_engine")["stale"])


class TestNeverRan(WatchdogSleepTestCase):
    def test_fresh_boot_is_not_a_hung_thread(self):
        """Nothing logged yet, machine up 2 minutes — that's a booting server."""
        rows = watchdog.check_agents(awake_min=2)
        self.assertEqual([r for r in rows if r["stale"]], [])

    def test_never_ran_after_a_long_uptime_is_stale(self):
        rows = watchdog.check_agents(awake_min=10_000)
        self.assertTrue(all(r["stale"] for r in rows))
        self.assertTrue(all(r["age_min"] is None for r in rows))


class TestUnknownWakeTime(WatchdogSleepTestCase):
    def test_falls_back_to_raw_age(self):
        """If wake time is unknowable, behave exactly as before rather than
        silently never restarting."""
        self._seen("analyst", "news_scan", 500)
        rows = watchdog.check_agents(awake_min=None)
        self.assertTrue(self._row(rows, "analyst")["stale"])

    def test_unknown_wake_time_with_fresh_agents_is_not_stale(self):
        self._seen("analyst", "news_scan", 5)
        rows = watchdog.check_agents(awake_min=None)
        self.assertFalse(self._row(rows, "analyst")["stale"])


class TestAwakeMinutesProbe(unittest.TestCase):
    def test_returns_a_sane_number_or_none(self):
        """Reads the real system clock — must never raise, whatever it finds."""
        v = watchdog.awake_minutes()
        if v is not None:
            self.assertGreaterEqual(v, 0.0)


if __name__ == "__main__":
    unittest.main()
