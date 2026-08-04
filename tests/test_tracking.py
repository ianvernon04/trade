"""Unit tests for the agent tracking store (no network, no pandas)."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import tracking


def fake_bars(start_price: float = 100.0, days: int = 40) -> list[tuple[str, float]]:
    """Deterministic daily bars: +1.0 per day starting 2026-07-01."""
    from datetime import date, timedelta
    d0 = date(2026, 7, 1)
    return [((d0 + timedelta(days=i)).isoformat(), start_price + i) for i in range(days)]


class TrackingTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "test.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()


class TestEvents(TrackingTestCase):
    def test_roundtrip_payload_and_filters(self):
        tracking.log_event("order", ticker="nvda", source="robinhood-mcp",
                           note="buy 10", payload={"id": "abc", "qty": 10})
        tracking.log_event("data", ticker="AAPL")
        rows = tracking.list_events(ticker="NVDA")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "NVDA")
        self.assertEqual(rows[0]["payload"], {"id": "abc", "qty": 10})
        self.assertEqual(len(tracking.list_events(kind="data")), 1)
        self.assertEqual(len(tracking.list_events()), 2)


class TestDecisions(TrackingTestCase):
    def test_validation(self):
        with self.assertRaises(ValueError):
            tracking.log_decision("NVDA", "yolo")
        with self.assertRaises(ValueError):
            tracking.log_decision("", "buy")

    def test_action_normalization_and_signal_json(self):
        d = tracking.log_decision("nvda", "No Trade", signal={"score": 3.2})
        self.assertEqual(d["action"], "no_trade")
        stored = tracking.list_decisions(ticker="NVDA")[0]
        self.assertEqual(stored["signal"], {"score": 3.2})
        self.assertIsNone(stored["evaluated_at"])
        self.assertEqual(len(tracking.list_decisions(pending_only=True)), 1)


class TestEvaluate(TrackingTestCase):
    def test_grades_directional_and_flat_calls(self):
        bars = fake_bars()  # rises 1.0/day from 100
        # Day index 5 = 2026-07-06 @ 105; +10 bars → 115 (+9.52%).
        tracking.log_decision("UP", "buy", horizon_days=10, price=105.0,
                              ts="2026-07-06T14:00:00Z")
        tracking.log_decision("UP", "sell", horizon_days=10, price=105.0,
                              ts="2026-07-06T15:00:00Z")
        tracking.log_decision("UP", "hold", horizon_days=10, price=105.0,
                              ts="2026-07-06T16:00:00Z")
        # Too close to the end of the series: must stay pending.
        tracking.log_decision("UP", "buy", horizon_days=10,
                              ts="2026-08-05T14:00:00Z")

        res = tracking.evaluate_decisions(lambda t: bars)
        self.assertEqual(res["evaluated"], 3)
        self.assertEqual(res["immature"], 1)
        self.assertEqual(res["errors"], {})

        graded = {d["action"]: d for d in tracking.list_decisions() if d["evaluated_at"]}
        self.assertEqual(graded["buy"]["hit"], 1)
        self.assertEqual(graded["sell"]["hit"], 0)
        self.assertIsNone(graded["hold"]["hit"])
        self.assertAlmostEqual(graded["buy"]["fwd_return_pct"], 9.52, places=2)
        self.assertEqual(graded["buy"]["eval_price"], 115.0)
        # Second run grades nothing new (immature one still immature).
        res2 = tracking.evaluate_decisions(lambda t: bars)
        self.assertEqual(res2["evaluated"], 0)
        self.assertEqual(res2["immature"], 1)

    def test_missing_price_uses_entry_bar_close(self):
        bars = fake_bars()
        # 2026-07-04 is bar index 3 (close 103); no recorded price → entry = 103.
        tracking.log_decision("UP", "buy", horizon_days=5, ts="2026-07-04T00:00:00Z")
        res = tracking.evaluate_decisions(lambda t: bars)
        self.assertEqual(res["evaluated"], 1)
        d = tracking.list_decisions()[0]
        self.assertAlmostEqual(d["fwd_return_pct"], round((108 / 103 - 1) * 100, 2), places=2)

    def test_bars_error_is_collected_not_raised(self):
        tracking.log_decision("BAD", "buy", ts="2026-07-06T00:00:00Z")

        def boom(t):
            raise RuntimeError("rate limited")

        res = tracking.evaluate_decisions(boom)
        self.assertEqual(res["evaluated"], 0)
        self.assertIn("BAD", res["errors"])
        self.assertEqual(len(tracking.list_decisions(pending_only=True)), 1)


class TestIngest(TrackingTestCase):
    def test_robinhood_order_envelope(self):
        payload = {"results": [
            {"side": "buy", "chain_symbol": "NVDA", "quantity": "10",
             "average_price": "181.20", "state": "filled", "id": "ord-1"},
            {"side": "sell", "symbol": "AAPL", "quantity": 5,
             "limit_price": 232.5, "state": "queued", "order_id": "ord-2"},
        ]}
        res = tracking.ingest(payload)
        self.assertEqual(res["stored"], 2)
        self.assertEqual(res["kinds"], {"fill": 1, "order": 1})
        fills = tracking.list_events(kind="fill")
        self.assertEqual(fills[0]["ticker"], "NVDA")
        self.assertIn("buy", fills[0]["note"])
        self.assertIn("181.2", fills[0]["note"])
        self.assertEqual(fills[0]["payload"]["id"], "ord-1")
        self.assertEqual(fills[0]["source"], "robinhood-mcp")

    def test_positions_and_option_fields(self):
        res = tracking.ingest(
            {"positions": [{"symbol": "MSFT", "quantity": "12", "average_buy_price": "412.10"}]})
        self.assertEqual(res["kinds"], {"position": 1})
        res = tracking.ingest(
            [{"side": "buy", "chain_symbol": "SPY", "quantity": 1, "option_type": "call",
              "strike_price": "560", "expiration_date": "2026-09-18", "price": 4.2,
              "state": "confirmed"}])
        note = tracking.list_events(kind="order")[0]["note"]
        self.assertIn("560 call 2026-09-18", note)

    def test_unknown_shapes_survive(self):
        self.assertEqual(tracking.ingest("plain text blob")["stored"], 1)
        self.assertEqual(tracking.ingest({"weird": {"nested": True}})["stored"], 1)
        self.assertEqual(tracking.ingest('{"symbol": "TSLA", "foo": 1}')["stored"], 1)
        self.assertEqual(tracking.list_events(ticker="TSLA")[0]["payload"]["foo"], 1)


class TestSummary(TrackingTestCase):
    def test_summary_shapes_and_track_record(self):
        bars = fake_bars()
        tracking.log_event("order", ticker="UP", source="robinhood-mcp")
        tracking.log_decision("UP", "buy", horizon_days=10, price=105.0,
                              ts="2026-07-06T14:00:00Z")
        tracking.log_decision("UP", "put", horizon_days=10, price=105.0,
                              ts="2026-07-06T15:00:00Z")
        tracking.evaluate_decisions(lambda t: bars)
        s = tracking.summary()
        self.assertEqual(s["events"]["total"], 1)
        self.assertEqual(s["decisions"]["graded"], 2)
        self.assertEqual(s["track_record"]["hit_rate"], 50.0)
        self.assertEqual(s["track_record"]["by_action"]["buy"]["hit_rate"], 100.0)
        self.assertEqual(s["track_record"]["by_action"]["put"]["hit_rate"], 0.0)
        self.assertEqual(s["track_record"]["best"]["action"], "buy")
        self.assertEqual(s["track_record"]["worst"]["action"], "put")
        self.assertEqual(len(s["recent_decisions"]), 2)

    def test_export(self):
        tracking.log_event("note", note="hello")
        tracking.log_decision("NVDA", "buy")
        dump = tracking.export_all()
        self.assertEqual(len(dump["events"]), 1)
        self.assertEqual(len(dump["decisions"]), 1)
        self.assertIn("exported_at", dump)


if __name__ == "__main__":
    unittest.main()
