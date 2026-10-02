"""Gate policy tests: the exact stand-downs from the August logs, re-decided.

Every case here is a real situation from autotrade.log. The old gate stood
down on all of them; the new policy must stand down on the dangerous ones and
only those.
"""

from __future__ import annotations

import unittest
from datetime import date

from app import gate


def macro_alert(topic="inflation", count=3, mid=1, ts="2026-08-17T08:33:00Z"):
    return {"id": mid, "ts": ts, "from_agent": "analyst", "to_agent": "trader",
            "priority": "high", "ticker": None,
            "subject": f"Macro risk in headlines: {topic}",
            "payload": {"topic": topic, "count": count, "headlines": []},
            "read_at": None}


def ticker_alert(ticker="NVDA", mid=2, ts="2026-08-17T09:00:00Z"):
    return {"id": mid, "ts": ts, "from_agent": "analyst", "to_agent": "trader",
            "priority": "high", "ticker": ticker,
            "subject": f"Negative news cluster: {ticker}",
            "payload": {"negative": 4, "positive": 1, "count": 5},
            "read_at": None}


# A quiet non-event weekday: next CPI is 2026-09-11, next FOMC 2026-09-16.
QUIET_DAY = date(2026, 8, 20)


class AmbientMacroTest(unittest.TestCase):
    """The failure mode: ambient headlines blocking every single run."""

    def test_small_macro_alert_does_not_block(self):
        # Aug 17's actual stand-down: one inflation alert, ordinary size.
        v = gate.classify([macro_alert("inflation", count=3)], today=QUIET_DAY)
        self.assertFalse(v["stand_down"])
        self.assertEqual(len(v["ambient"]), 1)
        self.assertEqual(v["blocking"], [])

    def test_several_small_macro_topics_still_do_not_block(self):
        # Aug 14's actual stand-down: fed/rates + inflation + tariffs + geo.
        rows = [macro_alert(t, count=c, mid=i) for i, (t, c) in enumerate(
            [("fed/rates", 3), ("inflation", 2), ("tariffs/trade", 4),
             ("geopolitics", 2)])]
        v = gate.classify(rows, today=QUIET_DAY)
        self.assertFalse(v["stand_down"])
        self.assertEqual(len(v["ambient"]), 4)

    def test_empty_window_is_clear(self):
        v = gate.classify([], today=QUIET_DAY)
        self.assertFalse(v["stand_down"])


class RealRiskStillBlocksTest(unittest.TestCase):
    def test_ticker_alert_blocks(self):
        v = gate.classify([ticker_alert("NVDA")], today=QUIET_DAY)
        self.assertTrue(v["stand_down"])
        self.assertIn("NVDA", v["blocking"][0]["why"])

    def test_headline_storm_blocks(self):
        v = gate.classify([macro_alert("geopolitics", count=9)], today=QUIET_DAY)
        self.assertTrue(v["stand_down"])
        self.assertIn("storm", v["blocking"][0]["why"])

    def test_storm_threshold_boundary(self):
        below = gate.classify([macro_alert(count=gate.STORM_MIN - 1)], today=QUIET_DAY)
        at = gate.classify([macro_alert(count=gate.STORM_MIN)], today=QUIET_DAY)
        self.assertFalse(below["stand_down"])
        self.assertTrue(at["stand_down"])

    def test_mixed_window_blocks_and_reports_both(self):
        v = gate.classify([macro_alert("inflation", 3, mid=1),
                           ticker_alert("AAPL", mid=2)], today=QUIET_DAY)
        self.assertTrue(v["stand_down"])
        self.assertEqual(len(v["blocking"]), 1)
        self.assertEqual(len(v["ambient"]), 1)

    def test_unrecognizable_alert_fails_closed(self):
        weird = {"id": 9, "ts": "2026-08-17T09:00:00Z", "ticker": None,
                 "subject": "Something new the policy has never seen",
                 "payload": None, "priority": "high", "read_at": None}
        v = gate.classify([weird], today=QUIET_DAY)
        self.assertTrue(v["stand_down"])
        self.assertIn("failing closed", v["blocking"][0]["why"])


class EventDayTest(unittest.TestCase):
    def test_cpi_day_blocks_even_with_no_alerts(self):
        v = gate.classify([], today=date(2026, 9, 11))
        self.assertTrue(v["stand_down"])
        self.assertEqual(v["event"]["kind"], "CPI")

    def test_day_before_fomc_blocks(self):
        v = gate.classify([], today=date(2026, 9, 15))
        self.assertTrue(v["stand_down"])
        self.assertEqual(v["event"], {"kind": "FOMC", "date": "2026-09-16",
                                      "phase": "tomorrow"})

    def test_day_after_event_is_clear_again(self):
        v = gate.classify([], today=date(2026, 9, 17))
        self.assertFalse(v["stand_down"])
        self.assertIsNone(v["event"])

    def test_event_window_helper(self):
        self.assertIsNone(gate.macro_event_window(date(2026, 8, 20)))
        self.assertIsNotNone(gate.macro_event_window(date(2026, 8, 12)))  # CPI day


class CliGateTest(unittest.TestCase):
    """The CLI wiring: --gate consults the policy; staleness fails closed."""

    def setUp(self):
        import tempfile
        from pathlib import Path

        from app import tracking
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        original = tracking.DB_PATH
        self.addCleanup(setattr, tracking, "DB_PATH", original)
        tracking.DB_PATH = Path(self.tmp.name) / "journal.db"
        tracking.init()
        self.tracking = tracking

    def _run(self, *argv) -> int:
        from app.agent_cli import main
        return main(list(argv))

    def test_gate_clear_on_ambient_macro(self):
        self.tracking.log_event("news_scan", source="analyst", note="scan")
        self.tracking.send_message(
            "analyst", "trader", "Macro risk in headlines: inflation",
            kind="alert", priority="high",
            payload={"topic": "inflation", "count": 3})
        # Skip on real event days — this test asserts the ambient path.
        from datetime import date as d
        if gate.macro_event_window(d.today()):
            self.skipTest("scheduled macro event day — gate correctly blocks")
        code = self._run("alerts", "--agent", "trader", "--from", "analyst",
                         "--gate", "--no-self-heal")
        self.assertEqual(code, 0)

    def test_gate_blocks_on_ticker_alert(self):
        self.tracking.log_event("news_scan", source="analyst", note="scan")
        self.tracking.send_message(
            "analyst", "trader", "Negative news cluster: NVDA",
            kind="alert", priority="high", ticker="NVDA",
            payload={"negative": 4, "positive": 0, "count": 4})
        code = self._run("alerts", "--agent", "trader", "--from", "analyst",
                         "--gate", "--no-self-heal")
        self.assertEqual(code, 1)

    def test_stale_scanner_fails_closed_without_self_heal(self):
        # No news_scan event at all → staleness path, no self-heal allowed.
        code = self._run("alerts", "--agent", "trader", "--from", "analyst",
                         "--gate", "--no-self-heal")
        self.assertEqual(code, 1)

    def test_default_mode_unchanged_blunt_behavior(self):
        self.tracking.log_event("news_scan", source="analyst", note="scan")
        self.tracking.send_message(
            "analyst", "trader", "Macro risk in headlines: inflation",
            kind="alert", priority="high",
            payload={"topic": "inflation", "count": 2})
        code = self._run("alerts", "--agent", "trader", "--from", "analyst")
        self.assertEqual(code, 1)  # without --gate, any alert still blocks


if __name__ == "__main__":
    unittest.main()
