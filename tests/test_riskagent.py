"""Unit tests for the Risk Manager: portfolio-level Greeks and exposure."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import riskagent, tracking


def p(ticker, delta_shares=0, theta_per_day=0, instrument="stock", quantity=1):
    """Position fixture."""
    return {
        "ticker": ticker, "instrument": instrument, "quantity": quantity,
        "delta_shares": delta_shares, "theta_per_day": theta_per_day,
        "dte": 30, "unrealized_pnl_pct": 0,
    }


class TestAnalyzePortfolio(unittest.TestCase):
    def test_empty_portfolio(self):
        a = riskagent.analyze_portfolio({}, [])
        self.assertEqual(a["n_positions"], 0)
        self.assertEqual(a["risk_level"], "low")

    def test_single_position_low_risk(self):
        totals = {"delta_shares": 100, "theta_per_day": 10}
        positions = [p("NVDA", delta_shares=100)]
        a = riskagent.analyze_portfolio(totals, positions)
        self.assertEqual(a["net_delta"], 100)
        self.assertEqual(a["risk_level"], "low")

    def test_concentration_risk_flagged(self):
        # Two positions, one dominates > 50% of delta.
        totals = {"delta_shares": 600, "theta_per_day": 10}
        positions = [p("AAPL", delta_shares=400), p("SPY", delta_shares=200)]
        a = riskagent.analyze_portfolio(totals, positions)
        conc_issues = [i for i in a["issues"] if i["type"] == "concentration"]
        self.assertGreater(len(conc_issues), 0)
        self.assertIn(a["risk_level"], ["medium", "high"])

    def test_theta_imbalance_detected(self):
        # Large theta decay (>$100/day).
        totals = {"delta_shares": 100, "theta_per_day": -150}  # short theta
        positions = [p("SPY", theta_per_day=-150)]
        a = riskagent.analyze_portfolio(totals, positions)
        theta_issues = [i for i in a["issues"] if i["type"] == "theta_imbalance"]
        self.assertGreater(len(theta_issues), 0)

    def test_high_leverage_flagged(self):
        # Gross delta > 2000 (equivalent of 20 shares per option).
        totals = {"delta_shares": 2400, "theta_per_day": 10}
        long_positions = [p(f"T{i}", delta_shares=400) for i in range(6)]
        a = riskagent.analyze_portfolio(totals, long_positions)
        lev_issues = [i for i in a["issues"] if i["type"] == "leverage"]
        self.assertGreater(len(lev_issues), 0)

    def test_unhedged_long_flagged(self):
        # High delta, minimal theta = naked long (no hedges).
        totals = {"delta_shares": 600, "theta_per_day": 2}  # long, minimal decay
        positions = [p("NVDA", delta_shares=600)]
        a = riskagent.analyze_portfolio(totals, positions)
        unhg_issues = [i for i in a["issues"] if i["type"] == "unhedged"]
        self.assertGreater(len(unhg_issues), 0)


class TestComposeMessages(unittest.TestCase):
    def test_high_priority_for_leverage(self):
        a = {
            "net_delta": 1500, "net_theta": 10.0, "gross_delta": 2500, "n_positions": 2,
            "risk_level": "high",
            "issues": [{"type": "leverage", "gross_delta": 2500, "note": "..."}],
        }
        msgs = riskagent.compose_messages(a)
        high_msgs = [m for m in msgs if m["priority"] == "high"]
        self.assertGreater(len(high_msgs), 0)

    def test_summary_message_created(self):
        a = {
            "net_delta": 500, "net_theta": 50.0, "gross_delta": 600, "n_positions": 3,
            "risk_level": "medium",
            "issues": [{"type": "concentration", "concentration_pct": 60, "note": "..."}],
        }
        msgs = riskagent.compose_messages(a)
        summary_msgs = [m for m in msgs if "Portfolio Greeks" in m.get("subject", "")]
        self.assertEqual(len(summary_msgs), 1)


class TestRunScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "r.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_scan_logs_event(self):
        res = riskagent.run_scan(send=False)
        events = tracking.list_events(kind="risk_scan")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "risk_manager")


if __name__ == "__main__":
    unittest.main()
