"""Unit tests for the Position Manager: position monitoring + exit signals."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import positionagent, tracking


def p(ticker, instrument="stock", strike=None, expiry=None, dte=None,
      unrealized_pnl_pct=None, theta_per_day=0, earnings_in_days=None):
    """Position fixture."""
    return {
        "id": 1, "ticker": ticker, "instrument": instrument, "strike": strike,
        "expiry": expiry, "dte": dte, "unrealized_pnl_pct": unrealized_pnl_pct,
        "theta_per_day": theta_per_day, "earnings_in_days": earnings_in_days,
        "quantity": 1, "mark": 100.0,
    }


class TestAnalyzePositions(unittest.TestCase):
    def test_empty_portfolio(self):
        a = positionagent.analyze_positions([])
        self.assertEqual(a["n_positions"], 0)
        self.assertEqual(a["theta_sum"], 0.0)
        self.assertEqual(len(a["actions"]), 0)

    def test_take_profit_alert_at_50_percent(self):
        a = positionagent.analyze_positions([p("NVDA", unrealized_pnl_pct=75)])
        self.assertEqual(len(a["actions"]), 1)
        actions_of_type = [ac for ac in a["actions"] if ac.get("type") == "take_profit"]
        self.assertEqual(len(actions_of_type), 1)
        self.assertEqual(actions_of_type[0]["pnl_pct"], 75)

    def test_reversal_alert_at_minus_50_percent(self):
        a = positionagent.analyze_positions([p("TSLA", unrealized_pnl_pct=-60)])
        actions_of_type = [ac for ac in a["actions"] if ac.get("type") == "reversal"]
        self.assertEqual(len(actions_of_type), 1)
        self.assertEqual(actions_of_type[0]["pnl_pct"], -60)

    def test_expiry_warning_within_7_days(self):
        a = positionagent.analyze_positions([p("AAPL", instrument="call", dte=5)])
        self.assertEqual(len(a["dte_warns"]), 1)
        self.assertEqual(a["dte_warns"][0][0], "AAPL")
        self.assertEqual(a["dte_warns"][0][1], 5)

    def test_earnings_warning_within_5_days(self):
        a = positionagent.analyze_positions([p("AMD", earnings_in_days=3)])
        self.assertEqual(len(a["earnings_warns"]), 1)
        self.assertEqual(a["earnings_warns"][0][0], "AMD")
        self.assertEqual(a["earnings_warns"][0][1], 3)

    def test_theta_decay_alert(self):
        a = positionagent.analyze_positions([p("SPY", theta_per_day=75)])
        self.assertEqual(a["theta_sum"], 75.0)
        # Should have an action for high theta.
        alerts_with_theta = [ac for ac in a["actions"] if any("theta" in alert.lower() for alert in ac.get("alerts", []))]
        self.assertGreater(len(alerts_with_theta), 0)


class TestComposeMessages(unittest.TestCase):
    def test_take_profit_message_created(self):
        a = {"n_positions": 1, "theta_sum": 10.0, "dte_warns": [], "earnings_warns": [],
             "actions": [{"type": "take_profit", "ticker": "NVDA", "pnl_pct": 75,
                          "note": "call @ 180 is up 75%"}]}
        msgs = positionagent.compose_messages(a)
        subjects = [m["subject"] for m in msgs]
        self.assertTrue(any("P&L" in s or "profit" in s.lower() for s in subjects))

    def test_reversal_alert_is_high_priority(self):
        a = {"n_positions": 1, "theta_sum": 10.0, "dte_warns": [], "earnings_warns": [],
             "actions": [{"type": "reversal", "ticker": "TSLA", "pnl_pct": -60,
                          "note": "call @ 190 down -60%"}]}
        msgs = positionagent.compose_messages(a)
        reversal_msgs = [m for m in msgs if "reversal" in m.get("subject", "").lower()]
        self.assertTrue(any(m["priority"] == "high" for m in reversal_msgs))

    def test_expiry_warning_message(self):
        pos = p("AAPL", instrument="call", dte=5)
        a = {"n_positions": 1, "theta_sum": 10.0,
             "dte_warns": [("AAPL", 5, pos)], "earnings_warns": [],
             "actions": []}
        msgs = positionagent.compose_messages(a)
        expiry_msgs = [m for m in msgs if "expiry" in m.get("subject", "").lower()]
        self.assertGreater(len(expiry_msgs), 0)

    def test_earnings_warning_message_is_high_priority(self):
        pos = p("AMD", earnings_in_days=3)
        a = {"n_positions": 1, "theta_sum": 10.0,
             "dte_warns": [], "earnings_warns": [("AMD", 3, pos)],
             "actions": []}
        msgs = positionagent.compose_messages(a)
        earn_msgs = [m for m in msgs if "earnings" in m.get("subject", "").lower()]
        self.assertTrue(any(m["priority"] == "high" for m in earn_msgs))


class TestRunScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "p.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_scan_logs_event_and_delivers_mail(self):
        # Mock positions for testing (run_scan tries to fetch from journal, which is empty,
        # so this mainly tests the logging/message flow).
        res = positionagent.run_scan(send=True)
        events = tracking.list_events(kind="position_scan")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "position_manager")

    def test_no_send_mode(self):
        res = positionagent.run_scan(send=False)
        self.assertEqual(res["delivered"], [])
        self.assertEqual(len(tracking.list_events(kind="position_scan")), 1)


if __name__ == "__main__":
    unittest.main()
