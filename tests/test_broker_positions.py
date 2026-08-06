"""Tests for reading positions out of the tracking store.

Background: the Position and Risk Managers read only `journal.trades`, the
human's hand-kept log. An account traded through the broker never appears
there, so both agents reported "0 positions / LOW risk" against a real
portfolio. A risk gauge that reads empty when it means blind is worse than
one that errors, so these tests pin the reconstruction rather than the
happy path.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import positions, tracking


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class BrokerPositionsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "t.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def ingest(self, payload: dict):
        return tracking.log_event("position", ticker=payload.get("symbol"),
                                  source="robinhood-mcp", payload=payload)


class TestReconstruction(BrokerPositionsTestCase):
    def test_equity_row_becomes_a_position(self):
        self.ingest({"symbol": "ZZETF", "quantity": "1000.0",
                     "average_buy_price": "50.00", "type": "long"})
        rows = positions._trade_rows_from_tracking()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["ticker"], "ZZETF")
        self.assertEqual(r["instrument"], "stock")
        self.assertAlmostEqual(r["entry_price"], 50.00)

    def test_option_average_price_is_divided_by_multiplier(self):
        # Broker reports per-contract dollars; the journal's entry_price is
        # per share. Getting this wrong inflates cost basis 100x.
        self.ingest({"symbol": "ZZOPT", "option_type": "call", "strike_price": "100.0",
                     "expiration_date": "2026-08-07", "quantity": "10",
                     "average_price": "1061.60", "trade_value_multiplier": "100",
                     "type": "long"})
        r = positions._trade_rows_from_tracking()[0]
        self.assertAlmostEqual(r["entry_price"], 10.616)
        self.assertEqual(r["instrument"], "call")
        self.assertEqual(r["strike"], 100.0)

    def test_zero_quantity_rows_are_closed_and_dropped(self):
        self.ingest({"symbol": "NVDA", "option_type": "call", "strike_price": "212.5",
                     "expiration_date": "2026-08-05", "quantity": "0",
                     "average_price": "231.00", "type": "long"})
        self.assertEqual(positions._trade_rows_from_tracking(), [])

    def test_chain_symbol_is_accepted_as_the_ticker(self):
        # Robinhood's option positions use chain_symbol, not symbol. Reading
        # only "symbol" silently dropped every option row.
        self.ingest({"chain_symbol": "NVDA", "option_type": "call",
                     "strike_price": "200", "expiration_date": "2026-09-18",
                     "quantity": "2", "average_price": "500",
                     "trade_value_multiplier": "100", "type": "long"})
        rows = positions._trade_rows_from_tracking()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ticker"], "NVDA")

    def test_short_direction_is_preserved(self):
        self.ingest({"symbol": "AAPL", "quantity": "10",
                     "average_buy_price": "200", "type": "short"})
        self.assertEqual(positions._trade_rows_from_tracking()[0]["direction"], "short")


class TestSnapshotSemantics(BrokerPositionsTestCase):
    """The newest batch replaces the set; it does not merge into it.

    Merging by contract cannot express "this position is gone". A closed
    contract often vanishes from the broker's list, or returns stripped of
    strike/expiry, so there is no newer row to overwrite the old one — and a
    dead position lives forever in the agents' view.
    """

    def _at(self, when: datetime, payload: dict):
        ev = self.ingest(payload)
        with tracking._conn() as c:
            c.execute("UPDATE agent_events SET ts=? WHERE id=?", (_iso(when), ev["id"]))

    def test_older_snapshot_is_ignored_entirely(self):
        old = datetime.now(timezone.utc) - timedelta(hours=6)
        new = datetime.now(timezone.utc)
        self._at(old, {"symbol": "MU", "quantity": "4", "average_buy_price": "1113", "type": "long"})
        self._at(new, {"symbol": "ZZETF", "quantity": "1000", "average_buy_price": "50.0", "type": "long"})
        tickers = {r["ticker"] for r in positions._trade_rows_from_tracking()}
        self.assertEqual(tickers, {"ZZETF"}, "a position absent from the newest batch is closed")

    def test_position_vanishing_from_newest_batch_disappears(self):
        old = datetime.now(timezone.utc) - timedelta(hours=6)
        new = datetime.now(timezone.utc)
        # Held this morning...
        self._at(old, {"symbol": "NVDA", "option_type": "call", "strike_price": "212.5",
                       "expiration_date": "2026-08-05", "quantity": "50",
                       "average_price": "231", "trade_value_multiplier": "100", "type": "long"})
        # ...and this evening the broker returns it with no strike at all.
        self._at(new, {"chain_symbol": "NVDA", "expiration_date": "2026-08-05",
                       "quantity": "0", "average_price": "231", "type": "long"})
        self.assertEqual(positions._trade_rows_from_tracking(), [])

    def test_options_and_equities_track_separate_batches(self):
        # One asset class being re-ingested must not wipe out the other.
        old = datetime.now(timezone.utc) - timedelta(hours=3)
        new = datetime.now(timezone.utc)
        self._at(old, {"symbol": "ZZOPT", "option_type": "call", "strike_price": "100",
                       "expiration_date": "2026-08-07", "quantity": "10",
                       "average_price": "1061.6", "trade_value_multiplier": "100",
                       "type": "long"})
        self._at(new, {"symbol": "ZZETF", "quantity": "1000", "average_buy_price": "50.0",
                       "type": "long"})
        got = {(r["ticker"], r["instrument"]) for r in positions._trade_rows_from_tracking()}
        self.assertEqual(got, {("ZZOPT", "call"), ("ZZETF", "stock")})


class TestStaleness(BrokerPositionsTestCase):
    def test_old_snapshot_is_flagged_not_silently_trusted(self):
        ev = self.ingest({"symbol": "ZZETF", "quantity": "1000",
                          "average_buy_price": "50.0", "type": "long"})
        stale = datetime.now(timezone.utc) - timedelta(hours=positions.STALE_AFTER_HOURS + 5)
        with tracking._conn() as c:
            c.execute("UPDATE agent_events SET ts=? WHERE id=?", (_iso(stale), ev["id"]))
        out = positions.broker_positions()
        self.assertTrue(out["stale"])
        self.assertTrue(any("old" in w for w in out["warnings"]))

    def test_fresh_snapshot_is_not_flagged(self):
        self.ingest({"symbol": "ZZETF", "quantity": "1000",
                     "average_buy_price": "50.0", "type": "long"})
        self.assertFalse(positions.broker_positions()["stale"])

    def test_empty_store_is_not_an_error(self):
        out = positions.broker_positions()
        self.assertEqual(out["positions"], [])
        self.assertFalse(out["stale"])


class TestComposeMessagesShapes(unittest.TestCase):
    """Generic monitoring actions carry no "type" key.

    compose_messages indexed act["type"] directly. That was unreachable while
    the journal stayed empty, and crashed the moment a real position produced
    a plain expiry or earnings warning.
    """

    def test_generic_action_without_type_does_not_crash(self):
        from app import positionagent
        analysis = {
            "n_positions": 1, "theta_sum": 0.0,
            "actions": [{"ticker": "ZZTOP", "instrument": "stock", "position_id": "rh:ZZTOP",
                         "strike": None, "expiry": None, "unrealized_pnl_pct": 3.2,
                         "alerts": ["earnings 5d away"]}],
            "dte_warns": [], "earnings_warns": [("ZZTOP", 5, {})],
        }
        msgs = positionagent.compose_messages(analysis)
        self.assertTrue(any("arnings" in m["subject"] for m in msgs))

    def test_typed_actions_still_produce_their_messages(self):
        from app import positionagent
        analysis = {
            "n_positions": 2, "theta_sum": 0.0,
            "actions": [
                {"type": "take_profit", "ticker": "NVDA", "position_id": "1",
                 "pnl_pct": 80.0, "note": "call up"},
                {"type": "reversal", "ticker": "MU", "position_id": "2",
                 "pnl_pct": -60.0, "note": "call down"},
            ],
            "dte_warns": [], "earnings_warns": [],
        }
        msgs = positionagent.compose_messages(analysis)
        subjects = " | ".join(m["subject"] for m in msgs)
        self.assertIn("NVDA", subjects)          # take_profit briefing
        self.assertIn("MU", subjects)            # reversal alert
        # The reversal is the one that must reach the Trader loudly.
        high = [m for m in msgs if m["priority"] == "high"]
        self.assertEqual([m["ticker"] for m in high], ["MU"])


if __name__ == "__main__":
    unittest.main()
