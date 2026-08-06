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


class TestUnpricedPositions(unittest.TestCase):
    """"$0/day theta" must never be how "we couldn't price it" renders."""

    def test_unpriced_positions_are_counted_not_treated_as_zero_decay(self):
        a = positionagent.analyze_positions([
            p("AAPL", theta_per_day=0.0),
            p("NVDA", instrument="call", theta_per_day=None),
            p("AMD", instrument="put", theta_per_day=None),
        ])
        self.assertEqual(a["unpriced"], 2)
        self.assertEqual(a["unpriced_tickers"], ["AMD", "NVDA"])

    def test_unpriced_raises_a_high_priority_alert(self):
        msgs = positionagent.compose_messages({
            "blind": False, "n_positions": 3, "theta_sum": 0.0, "unpriced": 2,
            "unpriced_tickers": ["AMD", "NVDA"], "source": "robinhood-mcp",
            "actions": [], "dte_warns": [], "earnings_warns": []})
        alert = next(m for m in msgs if m["priority"] == "high")
        self.assertIn("priced", alert["subject"].lower())
        self.assertIn("understates", alert["body"])

    def test_fully_priced_book_raises_no_such_alert(self):
        msgs = positionagent.compose_messages({
            "blind": False, "n_positions": 1, "theta_sum": -18.0, "unpriced": 0,
            "unpriced_tickers": [], "source": "robinhood-mcp",
            "actions": [], "dte_warns": [], "earnings_warns": []})
        self.assertFalse(any("priced" in m["subject"].lower() for m in msgs))


class TestBlindAndStale(unittest.TestCase):
    """No broker snapshot must never be reported as a quiet, empty book."""

    def test_blind_book_produces_one_high_priority_alert(self):
        msgs = positionagent.compose_messages(
            {"blind": True, "unreadable": 2, "n_positions": 0, "theta_sum": 0,
             "actions": [], "dte_warns": [], "earnings_warns": []})
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["priority"], "high")
        self.assertIn("blind", msgs[0]["subject"].lower())
        # It must tell the Trader how to fix it, not just complain.
        self.assertIn("ingest", msgs[0]["body"])

    def test_blind_suppresses_the_reassuring_summary(self):
        msgs = positionagent.compose_messages(
            {"blind": True, "unreadable": 0, "n_positions": 0, "theta_sum": 0,
             "actions": [], "dte_warns": [], "earnings_warns": []})
        self.assertFalse(any("Portfolio:" in m["subject"] for m in msgs))

    def test_genuinely_flat_book_is_quiet(self):
        # Seeing the account and finding nothing in it is not an alert.
        msgs = positionagent.compose_messages(
            {"blind": False, "n_positions": 0, "theta_sum": 0, "source": "robinhood-mcp",
             "actions": [], "dte_warns": [], "earnings_warns": []})
        self.assertEqual([m for m in msgs if m["priority"] == "high"], [])

    def test_stale_snapshot_alerts_but_still_reports(self):
        msgs = positionagent.compose_messages(
            {"blind": False, "stale": True, "age_hours": 52.0, "as_of": "2026-08-03T08:00:00Z",
             "n_positions": 3, "theta_sum": 20, "source": "robinhood-mcp",
             "actions": [], "dte_warns": [], "earnings_warns": []})
        self.assertTrue(any(m["priority"] == "high" and "stale" in m["subject"].lower()
                            for m in msgs))
        self.assertTrue(any("Portfolio:" in m["subject"] for m in msgs))


class TestRunScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "p.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    BOOK = {"source": "robinhood-mcp", "blind": False, "stale": False,
            "as_of": "2026-08-05T12:00:00Z", "age_hours": 0.5, "unreadable": 0,
            "totals": {"delta_shares": 110.0, "theta_per_day": -18.0},
            "positions": [p("NVDA", instrument="call", strike=185, dte=44,
                            unrealized_pnl_pct=38.6, theta_per_day=-18.0)]}

    def test_scan_logs_event_and_records_its_source(self):
        positionagent.run_scan(send=True, book=self.BOOK)
        events = tracking.list_events(kind="position_scan")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "position_manager")
        self.assertEqual(events[0]["payload"]["source"], "robinhood-mcp")
        self.assertEqual(events[0]["payload"]["n_positions"], 1)

    def test_blind_scan_mails_the_trader_a_high_priority_alert(self):
        res = positionagent.run_scan(
            send=True, book={"source": "none", "blind": True, "positions": [],
                             "totals": {}, "unreadable": 0})
        self.assertTrue(any(m["priority"] == "high" for m in res["delivered"]))
        unread = tracking.inbox("trader", unread_only=True)
        self.assertTrue(any("blind" in m["subject"].lower() for m in unread))

    def test_no_send_mode(self):
        res = positionagent.run_scan(send=False, book=self.BOOK)
        self.assertEqual(res["delivered"], [])
        self.assertEqual(len(tracking.list_events(kind="position_scan")), 1)


if __name__ == "__main__":
    unittest.main()
