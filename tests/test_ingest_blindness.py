"""The two ways an ingest can leave the agents blind while looking successful.

Both of these actually happened. A combined equity+option pull of a flat
account was stored as one unreadable row, so `broker_snapshot` reported
`blind` — the exact state the empty marker exists to prevent — while the
autonomous runs that did it logged "ingested as confirmed_empty" in good
faith. The Position and Risk Managers went quiet for two days.

The distinction under test is the one the whole portfolio module is built
around: *confirmed empty* (we looked, there is nothing) must never be
confused with *blind* (we cannot see), in either direction.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import portfolio, tracking


class IngestBlindnessTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "test.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()


class TestCombinedPositionPull(IngestBlindnessTestCase):
    """`{"equity_positions": [...], "option_positions": [...]}` — what the
    Trader naturally builds from two separate MCP tool calls."""

    def test_empty_combined_pull_is_confirmed_empty_not_blind(self):
        res = tracking.ingest(
            {"equity_positions": [], "option_positions": []},
            account="000000001")
        self.assertTrue(res["empty_marker"])

        snap = portfolio.broker_snapshot()
        self.assertTrue(snap["confirmed_empty"])
        self.assertFalse(snap["blind"])
        self.assertEqual(snap["unreadable"], 0)
        self.assertEqual(snap["accounts"], ["000000001"])

    def test_combined_pull_keeps_both_halves(self):
        tracking.ingest({
            "equity_positions": [
                {"symbol": "AAPL", "quantity": "10", "average_buy_price": "180"},
            ],
            "option_positions": [
                {"chain_symbol": "NVDA", "quantity": "2", "option_type": "call",
                 "strike_price": "185", "expiration_date": "2026-09-18",
                 "average_price": "1.20", "type": "long"},
            ],
        }, account="000000001")

        snap = portfolio.broker_snapshot()
        self.assertFalse(snap["blind"])
        self.assertEqual(snap["unreadable"], 0)
        tickers = sorted(p["ticker"] for p in snap["positions"])
        self.assertEqual(tickers, ["AAPL", "NVDA"])
        # The option leg keeps the fields the risk agents price off of.
        nvda = next(p for p in snap["positions"] if p["ticker"] == "NVDA")
        self.assertEqual(nvda["instrument"], "call")
        self.assertEqual(nvda["strike"], 185.0)
        self.assertEqual(nvda["expiry"], "2026-09-18")

    def test_one_empty_half_still_reports_the_other(self):
        """A flat options book alongside held equities is not an empty pull."""
        tracking.ingest({
            "equity_positions": [{"symbol": "MSFT", "quantity": "5",
                                  "average_buy_price": "400"}],
            "option_positions": [],
        }, account="000000001")

        snap = portfolio.broker_snapshot()
        self.assertFalse(snap["blind"])
        self.assertFalse(snap["confirmed_empty"])
        self.assertEqual([p["ticker"] for p in snap["positions"]], ["MSFT"])

    def test_empty_pull_supersedes_yesterdays_holdings(self):
        """The whole point of the marker: closed positions must not haunt."""
        tracking.ingest({"equity_positions": [
            {"symbol": "TSLA", "quantity": "3", "average_buy_price": "250"}]},
            account="000000001", ts="2026-08-11T14:00:00Z")
        tracking.ingest({"equity_positions": [], "option_positions": []},
                        account="000000001", ts="2026-08-12T14:00:00Z")

        snap = portfolio.broker_snapshot()
        self.assertEqual(snap["positions"], [])
        self.assertTrue(snap["confirmed_empty"])
        self.assertFalse(snap["blind"])


class TestUnnamedEmptyPayload(IngestBlindnessTestCase):
    def test_empty_payload_of_unknown_kind_records_nothing_and_warns(self):
        """Neither silently dropped nor silently asserted to be a flat book."""
        res = tracking.ingest({"results": []})
        self.assertEqual(res["stored"], 0)
        self.assertFalse(res["empty_marker"])
        self.assertIn("warning", res)

    def test_explicit_kind_still_marks_empty(self):
        res = tracking.ingest({"results": []}, kind_hint="positions",
                              account="000000001")
        self.assertTrue(res["empty_marker"])
        self.assertTrue(portfolio.broker_snapshot()["confirmed_empty"])


class TestBlindnessStillDetected(IngestBlindnessTestCase):
    def test_unreadable_position_payload_is_blind_not_empty(self):
        """The guard has to keep firing — this is the case it was built for."""
        tracking.log_event("position", source="robinhood-mcp",
                           note="held some things", payload={"nothing": "usable"})
        snap = portfolio.broker_snapshot()
        self.assertTrue(snap["blind"])
        self.assertFalse(snap["confirmed_empty"])
        self.assertEqual(snap["unreadable"], 1)


if __name__ == "__main__":
    unittest.main()
