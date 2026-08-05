"""Unit tests for the agents' view of the book.

The distinction these tests exist to protect: an empty portfolio and an
unreadable one must never produce the same answer. Reporting "flat" when the
truth is "I can't see it" is the failure mode that makes a risk agent worse
than none.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from app import portfolio, tracking

NOW = datetime(2026, 8, 5, 12, 0, 0, tzinfo=timezone.utc)


def ev(ts, payload, ticker=None):
    return {"ts": ts, "kind": "position", "ticker": ticker, "payload": payload}


RH_CALL = {"chain_symbol": "NVDA", "option_type": "call", "strike_price": "185.0",
           "expiration_date": "2026-09-18", "quantity": "2.0000",
           "average_price": "4.40", "type": "long"}
RH_STOCK = {"symbol": "AAPL", "quantity": "50.0000", "average_buy_price": "210.15"}


class TestNormalize(unittest.TestCase):
    def test_robinhood_option_leg(self):
        p = portfolio.normalize(RH_CALL)
        self.assertEqual(p["ticker"], "NVDA")
        self.assertEqual(p["instrument"], "call")
        self.assertEqual(p["strike"], 185.0)
        self.assertEqual(p["expiry"], "2026-09-18")
        self.assertEqual(p["quantity"], 2.0)
        self.assertEqual(p["direction"], "long")
        self.assertEqual(p["entry_price"], 4.40)

    def test_equity_position(self):
        p = portfolio.normalize(RH_STOCK)
        self.assertEqual(p["instrument"], "stock")
        self.assertEqual(p["quantity"], 50.0)
        self.assertEqual(p["entry_price"], 210.15)

    def test_short_by_label_and_by_sign(self):
        self.assertEqual(portfolio.normalize({**RH_CALL, "type": "short"})["direction"], "short")
        signed = portfolio.normalize({**RH_STOCK, "quantity": "-50"})
        self.assertEqual(signed["direction"], "short")
        self.assertEqual(signed["quantity"], 50.0)  # size is magnitude

    def test_unusable_payloads_rejected(self):
        self.assertIsNone(portfolio.normalize({"quantity": "5"}))       # no ticker
        self.assertIsNone(portfolio.normalize({"symbol": "NVDA"}))      # no quantity
        self.assertIsNone(portfolio.normalize({"symbol": "X", "quantity": "0"}))
        self.assertIsNone(portfolio.normalize("not a dict"))

    def test_expiry_with_timestamp_is_trimmed_to_a_date(self):
        p = portfolio.normalize({**RH_CALL, "expiration_date": "2026-09-18T00:00:00Z"})
        self.assertEqual(p["expiry"], "2026-09-18")


class TestSnapshot(unittest.TestCase):
    def test_no_events_is_blind_not_flat(self):
        s = portfolio.broker_snapshot(events=[], now=NOW)
        self.assertTrue(s["blind"])
        self.assertEqual(s["positions"], [])

    def test_newest_pull_defines_the_book(self):
        # An older pull held TSLA; today's does not, so TSLA is closed and
        # must not linger in the risk numbers.
        s = portfolio.broker_snapshot(events=[
            ev("2026-08-05T11:59:00Z", RH_CALL),
            ev("2026-08-05T11:59:01Z", RH_STOCK),
            ev("2026-08-01T09:00:00Z", {"symbol": "TSLA", "quantity": "10",
                                        "average_buy_price": "300"}),
        ], now=NOW)
        self.assertEqual({p["ticker"] for p in s["positions"]}, {"NVDA", "AAPL"})
        self.assertFalse(s["blind"])

    def test_duplicate_legs_collapse_to_one(self):
        s = portfolio.broker_snapshot(events=[
            ev("2026-08-05T11:59:00Z", RH_CALL),
            ev("2026-08-05T11:59:02Z", RH_CALL),
        ], now=NOW)
        self.assertEqual(len(s["positions"]), 1)

    def test_payloadless_events_are_blind_not_empty(self):
        # This is the shape `log position --note ...` leaves behind: a row
        # exists, but nothing readable. The agent knows something is held and
        # cannot say what — that is blindness.
        s = portfolio.broker_snapshot(
            events=[ev("2026-08-05T11:59:00Z", None, ticker="NVDA")], now=NOW)
        self.assertTrue(s["blind"])
        self.assertEqual(s["unreadable"], 1)

    def test_staleness_is_flagged(self):
        fresh = portfolio.broker_snapshot(
            events=[ev("2026-08-05T11:00:00Z", RH_CALL)], now=NOW)
        self.assertFalse(fresh["stale"])
        self.assertEqual(fresh["age_hours"], 1.0)

        old = portfolio.broker_snapshot(
            events=[ev("2026-08-03T11:00:00Z", RH_CALL)], now=NOW)
        self.assertTrue(old["stale"])
        self.assertGreater(old["age_hours"], portfolio.STALE_HOURS)


class TestEnrich(unittest.TestCase):
    """Market context the agents gather themselves, with the network injected."""

    def setUp(self):
        self.rows = [portfolio.normalize(RH_CALL), portfolio.normalize(RH_STOCK)]
        self.quote = lambda t: {"NVDA": 190.0, "AAPL": 220.0}[t]
        self.option = lambda p: {"mark": 6.10, "iv": 42.0, "delta": 0.55, "theta": -0.09}
        self.earnings = lambda t: 4 if t == "NVDA" else None

    def test_option_greeks_scale_to_share_equivalents(self):
        out = portfolio.enrich(self.rows, quote=self.quote, option=self.option,
                               earnings=self.earnings, today=date(2026, 8, 5))
        nvda = next(p for p in out if p["ticker"] == "NVDA")
        self.assertEqual(nvda["delta_shares"], 110.0)      # 0.55 × 2 × 100
        self.assertEqual(nvda["theta_per_day"], -18.0)     # -0.09 × 2 × 100
        self.assertEqual(nvda["dte"], 44)
        self.assertEqual(nvda["earnings_in_days"], 4)
        # 2 contracts, 4.40 → 6.10 = +$340 on $880 of premium.
        self.assertEqual(nvda["unrealized_pnl"], 340.0)
        self.assertAlmostEqual(nvda["unrealized_pnl_pct"], 38.6, places=1)

    def test_stock_is_one_delta_per_share_and_no_theta(self):
        out = portfolio.enrich(self.rows, quote=self.quote, option=self.option,
                               earnings=self.earnings, today=date(2026, 8, 5))
        aapl = next(p for p in out if p["ticker"] == "AAPL")
        self.assertEqual(aapl["delta_shares"], 50.0)
        self.assertEqual(aapl["theta_per_day"], 0.0)
        self.assertEqual(aapl["unrealized_pnl"], round((220.0 - 210.15) * 50, 2))

    def test_short_legs_invert_delta_and_theta(self):
        short = [portfolio.normalize({**RH_CALL, "type": "short"})]
        out = portfolio.enrich(short, quote=self.quote, option=self.option,
                               earnings=self.earnings, today=date(2026, 8, 5))
        self.assertEqual(out[0]["delta_shares"], -110.0)
        self.assertEqual(out[0]["theta_per_day"], 18.0)  # short collects theta

    def test_a_dead_chain_keeps_the_position_visible(self):
        def boom(_):
            raise RuntimeError("chain unavailable")
        out = portfolio.enrich(self.rows, quote=self.quote, option=boom,
                               earnings=self.earnings, today=date(2026, 8, 5))
        nvda = next(p for p in out if p["ticker"] == "NVDA")
        self.assertIsNone(nvda["delta_shares"])   # no greeks
        self.assertEqual(nvda["quantity"], 2.0)   # but the exposure is not lost

    def test_totals_sum_what_is_known(self):
        out = portfolio.enrich(self.rows, quote=self.quote, option=self.option,
                               earnings=self.earnings, today=date(2026, 8, 5))
        t = portfolio.totals(out)
        self.assertEqual(t["delta_shares"], 160.0)   # 110 + 50
        self.assertEqual(t["theta_per_day"], -18.0)


class TestCurrent(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "p.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old
        self._tmp.cleanup()

    def test_empty_store_reports_blind_with_no_positions(self):
        cur = portfolio.current(enrich_rows=False)
        self.assertTrue(cur["blind"])
        self.assertEqual(cur["source"], "none")
        self.assertEqual(cur["positions"], [])

    def test_ingested_broker_positions_become_the_book(self):
        tracking.ingest({"positions": [RH_CALL, RH_STOCK]}, source="robinhood-mcp")
        cur = portfolio.current(enrich_rows=False)
        self.assertEqual(cur["source"], "robinhood-mcp")
        self.assertFalse(cur["blind"])
        self.assertEqual({p["ticker"] for p in cur["positions"]}, {"NVDA", "AAPL"})


if __name__ == "__main__":
    unittest.main()
