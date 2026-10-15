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
        order = {**LONG_CALL, "limit_price": 4.50, "est_cost": 900, "max_loss": 900}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertEqual(v["computed_cost_usd"], 900)
        self.assertTrue(any("exceeds the per-trade" in r for r in v["reasons"]))

    def test_cap_boundary_is_inclusive(self):
        order = {**LONG_CALL, "limit_price": 1.50, "est_cost": 300, "max_loss": 300}
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
        order = {"ticker": "NVDA", "strategy": "naked_short_put", "side": "sell",
                 "quantity": 1, "strike": 2.5, "expiry": "2026-09-18",
                 "limit_price": 0.30, "max_loss": 250}
        v = autonomy.check_order(order, policy=self.enabled(defined_risk_only=False), now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertTrue(any("UNDEFINED RISK" in w for w in v["warnings"]))
        # Not a long strategy: the caller's max_loss is still the measure.
        self.assertEqual(v["worst_case_usd"], 250)

    def test_unknown_strategy_fails_closed(self):
        order = {"ticker": "NVDA", "strategy": "some_new_thing", "max_loss": 50}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertEqual(v["risk_kind"], "undefined")
        self.assertFalse(v["allowed"])

    def test_explicit_defined_risk_flag_wins(self):
        order = {"ticker": "NVDA", "strategy": "mystery", "defined_risk": True,
                 "quantity": 10, "limit_price": 5, "max_loss": 50}
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


class TestStrategyAllowlist(GateTestCase):
    """Robinhood's agentic accounts take long orders only, so a spread can be
    defined-risk and still unplaceable. The gate has to catch that here rather
    than let the broker reject it after a daily slot is spent."""

    def test_empty_allowlist_restricts_nothing(self):
        self.assertEqual(autonomy.DEFAULTS["allowed_strategies"], [])
        self.assertTrue(autonomy.check_order(LONG_CALL, policy=self.enabled(),
                                             now=NOW)["allowed"])

    def test_long_only_permits_long_call(self):
        policy = self.enabled(allowed_strategies=autonomy.LONG_ONLY)
        self.assertTrue(autonomy.check_order(LONG_CALL, policy=policy, now=NOW)["allowed"])

    def test_long_only_blocks_defined_risk_spread(self):
        policy = self.enabled(allowed_strategies=autonomy.LONG_ONLY)
        spread = {**LONG_CALL, "strategy": "debit_spread"}
        # It is genuinely defined-risk — that is the point of the test.
        self.assertEqual(autonomy.check_order(spread, policy=policy, now=NOW)["risk_kind"],
                         "defined")
        v = autonomy.check_order(spread, policy=policy, now=NOW)
        self.assertFalse(v["allowed"])
        self.assertTrue(any("allowlist" in r for r in v["reasons"]))

    def test_strategy_name_is_normalized_before_matching(self):
        policy = self.enabled(allowed_strategies=autonomy.LONG_ONLY)
        for spelling in ("Long Call", "LONG-CALL", " long_call "):
            order = {**LONG_CALL, "strategy": spelling}
            self.assertTrue(autonomy.check_order(order, policy=policy, now=NOW)["allowed"],
                            f"{spelling!r} should match long_call")

    def test_unspecified_strategy_denied_under_allowlist(self):
        policy = self.enabled(allowed_strategies=autonomy.LONG_ONLY)
        order = {k: v for k, v in LONG_CALL.items() if k != "strategy"}
        v = autonomy.check_order(order, policy=policy, now=NOW)
        self.assertFalse(v["allowed"])


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

    def test_strategies_persist_lowercased_not_uppercased(self):
        # Tickers upper, strategies lower — one list coercion can't do both.
        p = autonomy.save_policy({"allowed_strategies": "Long Call, LONG-PUT"})
        self.assertEqual(p["allowed_strategies"], ["long_call", "long_put"])


class TestOrderShape(GateTestCase):
    """The gate computes the worst case itself and refuses any order shape
    whose worst case it cannot compute. These include the three audit repros
    that the old, caller-trusting gate let through."""

    def deny(self, order, needle, **policy):
        v = autonomy.check_order(order, policy=self.enabled(**policy), now=NOW)
        self.assertFalse(v["allowed"], f"expected denial for {order}")
        self.assertTrue(any(needle in r for r in v["reasons"]),
                        f"no reason containing {needle!r} in {v['reasons']}")
        return v

    # --- audit repro 1: a $20,000 order claiming max_loss 0 ---
    def test_zero_max_loss_on_huge_order_denied(self):
        order = {"ticker": "NVDA", "strategy": "long_call", "side": "buy", "quantity": 50,
                 "expiry": "2026-09-18", "limit_price": 4.0, "max_loss": 0}
        v = self.deny(order, "not positive")
        self.assertEqual(v["computed_cost_usd"], 20000)
        self.assertEqual(v["worst_case_usd"], 20000)
        self.assertTrue(any("exceeds the per-trade" in r for r in v["reasons"]))

    # --- audit repro 2: selling on a "long" strategy ---
    def test_sell_side_on_long_strategy_denied(self):
        self.deny({**LONG_CALL, "side": "sell"}, "side must be 'buy'")
        self.deny({k: v for k, v in LONG_CALL.items() if k != "side"}, "side must be 'buy'")

    # --- audit repro 3: market orders and GTC ---
    def test_market_order_denied(self):
        self.deny({**LONG_CALL, "order_type": "market"}, "order_type 'market'")

    def test_gtc_denied(self):
        self.deny({**LONG_CALL, "time_in_force": "gtc"}, "time_in_force 'gtc'")

    def test_missing_order_type_and_tif_default_to_limit_day(self):
        v = autonomy.check_order(LONG_CALL, policy=self.enabled(), now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertEqual((v["order_type"], v["time_in_force"]), ("limit", "day"))
        gfd = {**LONG_CALL, "order_type": "LIMIT", "time_in_force": "gfd"}
        self.assertTrue(autonomy.check_order(gfd, policy=self.enabled(), now=NOW)["allowed"])

    def test_understated_max_loss_denied(self):
        # 1.20 × 2 × 100 = 240; claiming 100 is a lie the gate must catch.
        v = self.deny({**LONG_CALL, "max_loss": 100}, "understates")
        self.assertEqual(v["worst_case_usd"], 240)

    def test_overstated_max_loss_is_fine_but_cost_is_the_measure(self):
        v = autonomy.check_order({**LONG_CALL, "max_loss": 260}, policy=self.enabled(), now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertEqual(v["worst_case_usd"], 240)
        self.assertEqual(v["claimed_max_loss_usd"], 260)

    def test_rounding_tolerance_on_max_loss(self):
        self.assertTrue(autonomy.check_order({**LONG_CALL, "max_loss": 239.995},
                                             policy=self.enabled(), now=NOW)["allowed"])

    def test_est_cost_alone_no_longer_satisfies_max_loss(self):
        order = {k: v for k, v in LONG_CALL.items() if k != "max_loss"}
        self.deny(order, "no max_loss given")

    def test_bad_quantity_denied(self):
        for q in (0, -1, "two", None, float("nan"), True):
            self.deny({**LONG_CALL, "quantity": q}, "quantity must be a number")
        self.deny({**LONG_CALL, "quantity": 1.5}, "whole number of contracts")

    def test_fractional_shares_allowed_for_stock(self):
        order = {"ticker": "AAPL", "strategy": "stock", "side": "buy", "quantity": 0.5,
                 "limit_price": 200, "max_loss": 100}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertEqual(v["computed_cost_usd"], 100)
        self.assertFalse(v["is_option"])

    def test_bad_limit_price_denied(self):
        for p in (0, -4, "x", None, float("inf")):
            self.deny({**LONG_CALL, "limit_price": p}, "limit_price must be a number")

    def test_stock_cost_uses_no_multiplier(self):
        order = {"ticker": "AAPL", "strategy": "long_stock", "side": "buy", "quantity": 2,
                 "limit_price": 100, "max_loss": 200}
        v = autonomy.check_order(order, policy=self.enabled(), now=NOW)
        self.assertTrue(v["allowed"], v["reasons"])
        self.assertEqual(v["computed_cost_usd"], 200)
        self.assertEqual(v["multiplier"], 1)

    def test_expiry_required_and_must_be_future(self):
        self.deny({k: v for k, v in LONG_CALL.items() if k != "expiry"}, "must state an expiry")
        self.deny({**LONG_CALL, "expiry": "2026-08-05"}, "today or in the past")   # 0DTE
        self.deny({**LONG_CALL, "expiry": "2026-08-01"}, "today or in the past")
        self.deny({**LONG_CALL, "expiry": "Sept 18"}, "not a YYYY-MM-DD date")
        tomorrow = {**LONG_CALL, "expiry": "2026-08-06"}
        self.assertTrue(autonomy.check_order(tomorrow, policy=self.enabled(), now=NOW)["allowed"])

    def test_nan_max_loss_is_unbounded(self):
        v = self.deny({**LONG_CALL, "max_loss": float("nan")}, "unbounded")
        self.assertEqual(v["claimed_max_loss_usd"], "unbounded")

    def test_result_exposes_computed_numbers(self):
        v = autonomy.check_order(LONG_CALL, policy=self.enabled(), now=NOW)
        self.assertEqual(v["quantity"], 2)
        self.assertEqual(v["limit_price"], 1.2)
        self.assertEqual(v["multiplier"], 100)
        self.assertEqual(v["computed_cost_usd"], 240)
        self.assertEqual(v["expiry"], "2026-09-18")
        self.assertTrue(v["is_option"])


class TestRecording(GateTestCase):
    def test_recorded_order_is_auditable(self):
        ev = autonomy.record_autonomous_order(LONG_CALL)
        self.assertIn("warning", ev["note"])  # no reservation → flagged, still counted
        ev = tracking.list_events(kind="order")[0]
        self.assertEqual(ev["source"], "autonomous")
        self.assertEqual(ev["ticker"], "NVDA")
        self.assertIn("NVDA", ev["note"])
        self.assertEqual(ev["payload"]["strike"], 185)
        self.assertEqual(autonomy.trades_today(), 1)  # stamped now → counts today


class TestReservation(GateTestCase):
    """The slot is taken before the broker call and survives a crash."""

    def test_reserve_then_record_counts_once(self):
        policy = self.enabled(max_trades_per_day=2)
        res = autonomy.reserve_slot(LONG_CALL, policy=policy, now=NOW)
        self.assertTrue(res["reserved"], res["reasons"])
        rid = res["reservation_id"]
        self.assertIsInstance(rid, int)
        self.assertEqual(autonomy.trades_today(NOW), 1)
        row = tracking.list_events(kind="order")[0]
        self.assertEqual(row["id"], rid)
        self.assertEqual(row["payload"]["status"], "reserved")
        self.assertEqual(row["payload"]["ticker"], "NVDA")

        ev = autonomy.record_autonomous_order(LONG_CALL, payload={"id": "rh-1", "state": "queued"},
                                              reservation_id=rid)
        self.assertEqual(ev["id"], rid)
        self.assertEqual(autonomy.trades_today(NOW), 1)   # still one row
        rows = tracking.list_events(kind="order")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["status"], "placed")
        self.assertEqual(rows[0]["payload"]["broker_result"]["id"], "rh-1")
        self.assertEqual(rows[0]["payload"]["strike"], 185)
        self.assertNotIn("warning", rows[0]["note"])

    def test_record_without_order_uses_reserved_order(self):
        rid = autonomy.reserve_slot(LONG_CALL, policy=self.enabled(), now=NOW)["reservation_id"]
        ev = autonomy.record_autonomous_order(None, payload={"id": "rh-2"}, reservation_id=rid)
        self.assertEqual(ev["payload"]["ticker"], "NVDA")
        self.assertEqual(ev["payload"]["broker_result"], {"id": "rh-2"})

    def test_reserve_then_release_does_not_count(self):
        res = autonomy.reserve_slot(LONG_CALL, policy=self.enabled(), now=NOW)
        self.assertEqual(autonomy.trades_today(NOW), 1)
        out = autonomy.release_slot(res["reservation_id"], "broker tool unavailable")
        self.assertTrue(out["released"])
        self.assertEqual(autonomy.trades_today(NOW), 0)
        row = tracking.list_events(kind="order")[0]
        self.assertEqual(row["payload"]["status"], "released")
        self.assertEqual(row["payload"]["release_reason"], "broker tool unavailable")
        self.assertIn("RELEASED", row["note"])
        # The slot is free again.
        self.assertTrue(autonomy.check_order(LONG_CALL, policy=self.enabled(), now=NOW)["allowed"])
        # Idempotent.
        self.assertTrue(autonomy.release_slot(res["reservation_id"], "again")["already"])

    def test_crashed_reservation_still_counts(self):
        # Reserve, then "crash": never record, never release.
        autonomy.reserve_slot(LONG_CALL, policy=self.enabled(), now=NOW)
        self.assertEqual(autonomy.trades_today(NOW), 1)
        v = autonomy.check_order(LONG_CALL, policy=self.enabled(), now=NOW)
        self.assertFalse(v["allowed"])
        self.assertTrue(any("daily autonomous trade cap" in r for r in v["reasons"]))
        second = autonomy.reserve_slot(LONG_CALL, policy=self.enabled(), now=NOW)
        self.assertFalse(second["reserved"])
        self.assertIsNone(second["reservation_id"])

    def test_placed_reservation_cannot_be_released(self):
        rid = autonomy.reserve_slot(LONG_CALL, policy=self.enabled(), now=NOW)["reservation_id"]
        autonomy.record_autonomous_order(LONG_CALL, payload={"id": "x"}, reservation_id=rid)
        out = autonomy.release_slot(rid, "oops")
        self.assertFalse(out["released"])
        self.assertEqual(autonomy.trades_today(NOW), 1)

    def test_release_unknown_reservation_raises(self):
        with self.assertRaises(ValueError):
            autonomy.release_slot(999, "nope")

    def test_denied_order_never_reserves(self):
        res = autonomy.reserve_slot({**LONG_CALL, "side": "sell"}, policy=self.enabled(), now=NOW)
        self.assertFalse(res["reserved"])
        self.assertEqual(tracking.list_events(kind="order"), [])
        self.assertTrue(any("side must be 'buy'" in r for r in res["reasons"]))

    def test_record_with_missing_reservation_falls_back_with_warning(self):
        ev = autonomy.record_autonomous_order(LONG_CALL, payload={"id": "x"}, reservation_id=4242)
        self.assertIn("4242", ev["note"])
        self.assertIn("warning", ev["note"])
        self.assertEqual(len(tracking.list_events(kind="order")), 1)

    def test_legacy_rows_without_status_count(self):
        tracking.log_event("order", ticker="NVDA", source=autonomy.AUTONOMOUS_SOURCE,
                           ts="2026-08-05T10:00:00Z", payload="raw text")
        self.assertEqual(autonomy.trades_today(NOW), 1)


if __name__ == "__main__":
    unittest.main()
