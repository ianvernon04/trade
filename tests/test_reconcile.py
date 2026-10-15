"""Tests for broker-order reconciliation and the reserve/record/release CLI.

Reconciliation replaces the model reading `get_*_orders` JSON by eye in
step 6 of the autonomous routine: the broker's list goes in, an exit code
comes out. Every test here uses a temp DB and no network.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import agent_cli, autonomy, tracking

NOW = datetime(2026, 8, 28, 15, 0, tzinfo=timezone.utc)


def stamp(hours_ago: float) -> str:
    return (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def order(oid, state, hours_ago=2.0, **extra):
    return {"id": oid, "state": state, "created_at": stamp(hours_ago),
            "chain_symbol": "NVDA", "side": "buy", "quantity": "2.0", **extra}


class ReconcileTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db, self._old_policy = tracking.DB_PATH, autonomy.POLICY_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "t.db"
        autonomy.POLICY_PATH = Path(self._tmp.name) / "autonomy.json"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH, autonomy.POLICY_PATH = self._old_db, self._old_policy
        self._tmp.cleanup()

    def note_events(self):
        return tracking.list_events(kind="note")


class TestReconcile(ReconcileTestCase):
    def test_nothing_to_reconcile_is_clear(self):
        r = autonomy.reconcile_orders([], now=NOW)
        self.assertTrue(r["ok"])
        self.assertEqual(r["parsed"], 0)
        # Audit trail: the parse is recorded even when empty.
        self.assertEqual(len(self.note_events()), 1)
        self.assertEqual(self.note_events()[0]["payload"]["open_orders"], [])

    def test_open_order_blocks(self):
        for state in ("queued", "unconfirmed", "confirmed", "partially_filled",
                      "open", "pending"):
            r = autonomy.reconcile_orders([order("o1", state)], now=NOW)
            self.assertFalse(r["ok"], state)
            self.assertEqual(r["open_orders"][0]["id"], "o1")
            self.assertTrue(any("open orders exist" in x for x in r["reasons"]))

    def test_unknown_state_fails_closed(self):
        r = autonomy.reconcile_orders([order("o1", "something_new")], now=NOW)
        self.assertFalse(r["ok"])
        r = autonomy.reconcile_orders([{"id": "o2", "chain_symbol": "NVDA"}], now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["open_orders"][0]["why"], "unknown state")

    def test_old_open_order_still_blocks(self):
        # Monday's resting limit is still live on Thursday; age is no excuse.
        r = autonomy.reconcile_orders([order("o1", "confirmed", hours_ago=24 * 9)], now=NOW)
        self.assertFalse(r["ok"])

    def test_unrecorded_fill_is_logged_and_blocks(self):
        r = autonomy.reconcile_orders([order("o9", "filled")], now=NOW)
        self.assertFalse(r["ok"])
        self.assertTrue(any("unrecorded fill found" in x for x in r["reasons"]))
        self.assertEqual(r["unrecorded_fills"][0]["id"], "o9")
        rows = tracking.list_events(kind="order")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["source"], "autonomous")
        self.assertEqual(rows[0]["ticker"], "NVDA")
        self.assertEqual(rows[0]["payload"]["status"], "reconciled_unrecorded")
        self.assertEqual(rows[0]["payload"]["broker_order"]["id"], "o9")
        # It now counts toward today's cap (stamped with the real clock).
        self.assertEqual(autonomy.trades_today(), 1)

    def test_reconcile_is_idempotent(self):
        autonomy.reconcile_orders([order("o9", "filled")], now=NOW)
        r = autonomy.reconcile_orders([order("o9", "filled")], now=NOW)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(len(r["matched"]), 1)
        self.assertEqual(len(tracking.list_events(kind="order")), 1)

    def test_fill_matched_to_autonomous_record_is_clear(self):
        rid = autonomy.reserve_slot(
            {"ticker": "NVDA", "strategy": "long_call", "side": "buy", "quantity": 2,
             "expiry": "2026-09-18", "limit_price": 1.2, "max_loss": 240},
            policy={**autonomy.DEFAULTS, "enabled": True}, now=NOW)["reservation_id"]
        autonomy.record_autonomous_order(None, payload={"id": "o9", "state": "queued"},
                                         reservation_id=rid)
        r = autonomy.reconcile_orders([order("o9", "filled")], now=NOW)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(r["matched"][0]["id"], "o9")
        self.assertEqual(len(tracking.list_events(kind="order")), 1)

    def test_fill_matched_to_human_logged_order_is_clear(self):
        # CLAUDE.md rule 4: a human-approved order is logged with its payload.
        tracking.log_event("order", ticker="NVDA", source="robinhood-mcp",
                           payload={"order_id": "h1", "state": "confirmed"})
        r = autonomy.reconcile_orders([order("h1", "filled")], now=NOW)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(autonomy.trades_today(), 0)  # never became autonomous

    def test_terminal_non_fills_and_old_fills_are_ignored(self):
        orders = [order("c1", "cancelled"), order("r1", "rejected"),
                  order("x1", "expired"), order("old", "filled", hours_ago=24 * 5)]
        r = autonomy.reconcile_orders(orders, now=NOW)
        self.assertTrue(r["ok"], r["reasons"])
        self.assertEqual(r["ignored"], 4)
        self.assertEqual(tracking.list_events(kind="order"), [])

    def test_fill_without_timestamp_is_assumed_recent(self):
        o = {"id": "nots", "state": "filled", "chain_symbol": "NVDA"}
        r = autonomy.reconcile_orders([o], now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["unrecorded_fills"][0]["id"], "nots")

    def test_mixed_result_reports_both(self):
        r = autonomy.reconcile_orders([order("a", "queued"), order("b", "filled")], now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(len(r["reasons"]), 2)
        self.assertEqual(self.note_events()[0]["payload"]["open_orders"][0]["id"], "a")
        self.assertIn("STAND DOWN", self.note_events()[0]["note"])


class TestParsing(ReconcileTestCase):
    def test_list(self):
        self.assertEqual(len(autonomy.parse_broker_orders([order("a", "filled")])), 1)

    def test_equity_and_option_lists(self):
        payload = {"equity_orders": [order("e1", "queued")],
                   "option_orders": [order("o1", "filled"), order("o2", "cancelled")]}
        self.assertEqual([o["id"] for o in autonomy.parse_broker_orders(payload)],
                         ["e1", "o1", "o2"])

    def test_orders_key_and_nested_data_results(self):
        self.assertEqual(len(autonomy.parse_broker_orders({"orders": [order("a", "queued")]})), 1)
        nested = {"data": {"results": [order("a", "queued"), order("b", "queued")]}}
        self.assertEqual(len(autonomy.parse_broker_orders(nested)), 2)

    def test_json_string_and_single_order(self):
        text = json.dumps({"equity_orders": [order("a", "queued")], "option_orders": []})
        self.assertEqual(len(autonomy.parse_broker_orders(text)), 1)
        self.assertEqual(len(autonomy.parse_broker_orders(order("solo", "filled"))), 1)
        self.assertEqual(autonomy.parse_broker_orders("not json"), [])

    def test_order_id_and_status_aliases(self):
        o = {"order_id": "alias", "status": "Partially_Filled", "symbol": "aapl",
             "updated_at": stamp(1)}
        r = autonomy.reconcile_orders([o], now=NOW)
        self.assertFalse(r["ok"])
        self.assertEqual(r["open_orders"][0]["id"], "alias")
        self.assertEqual(r["open_orders"][0]["state"], "partially_filled")
        self.assertEqual(r["open_orders"][0]["ticker"], "AAPL")

    def test_two_lists_wrapped_in_a_list(self):
        # The model may paste both tool results back to back.
        payload = [{"equity_orders": [order("e", "queued")]}, {"option_orders": [order("o", "filled")]}]
        self.assertEqual([o["id"] for o in autonomy.parse_broker_orders(payload)], ["e", "o"])


class TestCli(ReconcileTestCase):
    LONG_CALL = json.dumps({"ticker": "NVDA", "strategy": "long_call", "side": "buy",
                            "quantity": 2, "strike": 185, "expiry": "2099-01-15",
                            "limit_price": 1.2, "max_loss": 240})

    def run_cli(self, *argv: str) -> tuple[int, str]:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            code = agent_cli.main(["--db", str(tracking.DB_PATH),
                                   "--policy-file", str(autonomy.POLICY_PATH), *argv])
        return code, buf.getvalue()

    def test_reconcile_exit_codes(self):
        code, out = self.run_cli("autonomy", "reconcile", "--orders", "[]")
        self.assertEqual(code, 0)
        self.assertIn("CLEAR", out)

        f = Path(self._tmp.name) / "orders.json"
        f.write_text(json.dumps({"option_orders": [order("q", "queued")]}))
        code, out = self.run_cli("autonomy", "reconcile", "--orders", f"@{f}")
        self.assertEqual(code, 1)
        self.assertIn("OPEN  q", out)
        self.assertIn("open orders exist", out)

        # The CLI uses the real clock, so stamp this one with it.
        fresh = {**order("f", "filled"),
                 "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        code, out = self.run_cli("autonomy", "reconcile", "--orders", json.dumps([fresh]))
        self.assertEqual(code, 1)
        self.assertIn("UNRECORDED FILL f", out)

        code, _ = self.run_cli("autonomy", "reconcile", "--orders", "[]", "--json")
        self.assertEqual(code, 0)

    def test_reconcile_requires_orders(self):
        code, _ = self.run_cli("autonomy", "reconcile")
        self.assertEqual(code, 2)

    def test_reserve_record_release_flow(self):
        self.run_cli("autonomy", "enable")
        code, out = self.run_cli("autonomy", "reserve", "--payload", self.LONG_CALL, "--json")
        self.assertEqual(code, 0)
        rid = json.loads(out)["reservation_id"]

        # Cap is 1/day: a second reserve is denied at once.
        code, out = self.run_cli("autonomy", "reserve", "--payload", self.LONG_CALL)
        self.assertEqual(code, 1)
        self.assertIn("DENIED", out)

        code, out = self.run_cli("autonomy", "record", "--reservation", str(rid),
                                 "--payload", '{"id": "rh-7", "state": "queued"}')
        self.assertEqual(code, 0)
        rows = tracking.list_events(kind="order")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["payload"]["status"], "placed")

        # Placed → cannot be released.
        code, out = self.run_cli("autonomy", "release", "--reservation", str(rid),
                                 "--reason", "x")
        self.assertEqual(code, 1)
        self.assertIn("NOT RELEASED", out)

    def test_release_frees_the_slot(self):
        self.run_cli("autonomy", "enable")
        code, out = self.run_cli("autonomy", "reserve", "--payload", self.LONG_CALL)
        self.assertEqual(code, 0)
        self.assertIn("RESERVED #1", out)
        code, out = self.run_cli("autonomy", "release", "--reservation", "1",
                                 "--reason", "tool unavailable")
        self.assertEqual(code, 0)
        self.assertIn("RELEASED #1", out)
        code, _ = self.run_cli("autonomy", "reserve", "--payload", self.LONG_CALL)
        self.assertEqual(code, 0)

    def test_release_usage_errors(self):
        self.assertEqual(self.run_cli("autonomy", "release", "--reservation", "1")[0], 2)
        self.assertEqual(self.run_cli("autonomy", "release", "--reservation", "77",
                                      "--reason", "x")[0], 2)  # not found → ValueError → 2

    def test_record_without_reservation_needs_order(self):
        self.assertEqual(self.run_cli("autonomy", "record", "--payload", "{}")[0], 2)
        code, out = self.run_cli("autonomy", "record", "--payload", '{"id": "z"}',
                                 "--order", self.LONG_CALL)
        self.assertEqual(code, 0)
        self.assertIn("warning", out)
        self.assertEqual(autonomy.trades_today(), 1)

    def test_check_denies_audit_repro(self):
        self.run_cli("autonomy", "enable")
        bad = json.dumps({"ticker": "NVDA", "strategy": "long_call", "side": "buy",
                          "quantity": 50, "expiry": "2099-01-15", "limit_price": 4.0,
                          "max_loss": 0})
        code, out = self.run_cli("autonomy", "check", "--payload", bad)
        self.assertEqual(code, 1)
        self.assertIn("20,000.00", out)


if __name__ == "__main__":
    unittest.main()
