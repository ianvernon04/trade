"""Unit tests for the unattended-trading risk gate.

Every test drives the gate the way an autonomous run would: build an order
dict, ask check_order, assert on the verdict. No network, no pandas.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app import autonomy, tracking

NOW = datetime(2026, 8, 5, 14, 0, tzinfo=timezone.utc)

LONG_CALL = {"ticker": "NVDA", "strategy": "long_call", "side": "buy", "quantity": 2,
             "strike": 185, "expiry": "2026-09-18", "limit_price": 1.20,
             "est_cost": 240, "max_loss": 240}


class GateTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db, self._old_policy = tracking.DB_PATH, autonomy.POLICY_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "t.db"
        autonomy.POLICY_PATH = Path(self._tmp.name) / "autonomy.json"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH, autonomy.POLICY_PATH = self._old_db, self._old_policy
        self._tmp.cleanup()

    def enabled(self, **overrides):
        return {**autonomy.DEFAULTS, "enabled": True, **overrides}


class TestKillSwitch(GateTestCase):
    def test_disabled_by_default(self):
        self.assertFalse(autonomy.load_policy()["enabled"])
        v = autonomy.check_order(LONG_CALL, now=NOW)
        self.assertFalse(v["allowed"])
        self.assertTrue(any("disabled" in r for r in v["reasons"]))

    def test_enabling_allows_a_conforming_order(self):
        autonomy.save_policy({"enabled": True})
        v = autonomy.check_order(LONG_CALL, now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertEqual(v["reasons"], [])
        self.assertEqual(v["worst_case_usd"], 240)
        self.assertEqual(v["risk_kind"], "defined")


class TestCaps(GateTestCase):
    def test_per_trade_cap(self):
        order = {**LONG_CALL, "est_cost": 900, "max_loss": 900}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertTrue(any("exceeds the per-trade" in r for r in v["reasons"]))

    def test_cap_boundary_is_inclusive(self):
        order = {**LONG_CALL, "est_cost": 300, "max_loss": 300}
        self.assertTrue(autonomy.check_order(order, policy=self.enabled(), now=NOW)["allowed"])

    def test_daily_cap_counts_only_autonomous_orders(self):
        policy = self.enabled(max_trades_per_day=2)
        # A human-approved order must NOT consume the autonomous budget.
        tracking.log_event("order", ticker="AAPL", source="robinhood-mcp",
                           ts="2026-08-05T10:00:00Z")
        self.assertTrue(autonomy.check_order(LONG_CALL, policy=policy, now=NOW)["allowed"])

        autonomy.record_autonomous_order({**LONG_CALL, "ticker": "AAPL"})
        autonomy.record_autonomous_order({**LONG_CALL, "ticker": "MSFT"})
        v = autonomy.check_order(LONG_CALL, policy=policy, now=NOW)
        self.assertFalse(v["allowed"])
        self.assertEqual(v["trades_today"], 2)
        self.assertTrue(any("daily autonomous trade cap" in r for r in v["reasons"]))

    def test_yesterdays_autonomous_orders_dont_count(self):
        tracking.log_event("order", ticker="NVDA", source=autonomy.AUTONOMOUS_SOURCE,
                           ts="2026-08-04T18:00:00Z")
        tracking.log_event("order", ticker="NVDA", source=autonomy.AUTONOMOUS_SOURCE,
                           ts="2026-08-04T19:00:00Z")
        v = autonomy.check_order(LONG_CALL, policy=self.enabled(max_trades_per_day=2), now=NOW)
        self.assertEqual(v["trades_today"], 0)
        self.assertTrue(v["allowed"], v["reasons"])


class TestRiskClassification(GateTestCase):
    def test_naked_call_blocked_when_defined_risk_only(self):
        order = {"ticker": "NVDA", "strategy": "naked_short_call", "quantity": 1,
                 "est_cost": 100, "max_loss": 100}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertEqual(v["risk_kind"], "undefined")
        self.assertTrue(any("undefined-risk" in r for r in v["reasons"]))

    def test_naked_allowed_when_policy_permits_but_warns_loudly(self):
        order = {"ticker": "NVDA", "strategy": "naked_short_put", "quantity": 1,
                 "max_loss": 250}
        v = autonomy.check_order(order, policy=self.enabled(defined_risk_only=False), now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertTrue(any("UNDEFINED RISK" in w for w in v["warnings"]))

    def test_unknown_strategy_fails_closed(self):
        order = {"ticker": "NVDA", "strategy": "some_new_thing", "max_loss": 50}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertEqual(v["risk_kind"], "undefined")
        self.assertFalse(v["allowed"])

    def test_explicit_defined_risk_flag_wins(self):
        order = {"ticker": "NVDA", "strategy": "mystery", "defined_risk": True, "max_loss": 50}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertEqual(v["risk_kind"], "defined")
        self.assertTrue(v["allowed"], v["reasons"])


class TestWorstCase(GateTestCase):
    def test_missing_max_loss_denied(self):
        order = {"ticker": "NVDA", "strategy": "long_call", "quantity": 1}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertIsNone(v["worst_case_usd"])
        self.assertTrue(any("worst case" in r for r in v["reasons"]))

    def test_infinite_max_loss_always_denied(self):
        order = {"ticker": "NVDA", "strategy": "long_call", "max_loss": float("inf")}
        v = autonomy.check_order(order, policy=self.enabled(defined_risk_only=False), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertEqual(v["worst_case_usd"], "unbounded")

    def test_max_loss_preferred_over_est_cost(self):
        order = {"ticker": "NVDA", "strategy": "credit_spread",
                 "est_cost": 50, "max_loss": 450}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])  # 450 > 300 cap, even though cost is 50
        self.assertEqual(v["worst_case_usd"], 450)


class TestAllowlist(GateTestCase):
    def test_allowlist_restricts_tickers(self):
        policy = self.enabled(allowed_tickers=["SPY", "QQQ"])
        self.assertFalse(autonomy.check_order(LONG_CALL, policy=policy, now=NOW)["allowed"])
        spy = {**LONG_CALL, "ticker": "SPY"}
        self.assertTrue(autonomy.check_order(spy, policy=policy, now=NOW)["allowed"])

    def test_missing_ticker_denied(self):
        v = autonomy.check_order({"strategy": "long_call", "max_loss": 10},
                                 policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertTrue(any("no ticker" in r for r in v["reasons"]))


class TestPolicyPersistence(GateTestCase):
    def test_roundtrip_and_coercion(self):
        autonomy.save_policy({"per_trade_max_usd": "500", "max_trades_per_day": "3",
                              "defined_risk_only": "false"})
        p = autonomy.load_policy()
        self.assertEqual(p["per_trade_max_usd"], 500.0)
        self.assertEqual(p["max_trades_per_day"], 3)
        self.assertIs(p["defined_risk_only"], False)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            autonomy.save_policy({"yolo_mode": True})

    def test_negative_cap_rejected(self):
        with self.assertRaises(ValueError):
            autonomy.save_policy({"per_trade_max_usd": -5})

    def test_corrupt_policy_file_falls_back_to_defaults(self):
        autonomy.POLICY_PATH.write_text("{not json")
        self.assertEqual(autonomy.load_policy(), autonomy.DEFAULTS)

    def test_allowlist_accepts_comma_string(self):
        p = autonomy.save_policy({"allowed_tickers": "spy, qqq"})
        self.assertEqual(p["allowed_tickers"], ["SPY", "QQQ"])


class TestRecording(GateTestCase):
    def test_recorded_order_is_auditable(self):
        autonomy.record_autonomous_order(LONG_CALL)
        ev = tracking.list_events(kind="order")[0]
        self.assertEqual(ev["source"], "autonomous")
        self.assertEqual(ev["ticker"], "NVDA")
        self.assertIn("NVDA", ev["note"])
        self.assertEqual(ev["payload"]["strike"], 185)


if __name__ == "__main__":
    unittest.main()
