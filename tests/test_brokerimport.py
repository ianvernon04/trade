"""Broker importer tests: stdlib only, no network, no real statements.

The fixtures below are hand-built to look like Robinhood's activity CSV,
including the parts that break naive parsers: dollar signs, thousands commas,
parenthesised debits, partial fills, expirations, assignments, and an exit whose
entry happened before the export window.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import brokerimport as bi
from app import journal

HEADER = ('"Activity Date","Process Date","Settle Date","Instrument",'
          '"Description","Trans Code","Quantity","Price","Amount"\n')


def row(date, instrument, description, code, qty, price, amount):
    return f'"{date}","{date}","{date}","{instrument}","{description}","{code}","{qty}","{price}","{amount}"\n'


# A clean round trip: buy 2 NVDA calls at 2.35, sell both at 3.10.
ROUND_TRIP = HEADER + (
    row("8/12/2026", "NVDA", "NVDA 8/15/2026 Call $180.00", "BTO", "2", "$2.35", "($470.00)")
    + row("8/14/2026", "NVDA", "NVDA 8/15/2026 Call $180.00", "STC", "2", "$3.10", "$619.94")
)


class ParseCsvTest(unittest.TestCase):
    def test_parses_an_option_round_trip(self):
        fills = bi.parse_activity_csv(ROUND_TRIP)
        self.assertEqual(len(fills), 2)
        buy, sell = fills
        self.assertEqual(buy.ticker, "NVDA")
        self.assertEqual(buy.instrument, "call")
        self.assertEqual(buy.strike, 180.0)
        self.assertEqual(buy.expiry, "2026-08-15")
        self.assertEqual(buy.date, "2026-08-12")
        self.assertEqual(buy.quantity, 2)
        self.assertEqual(buy.price, 2.35)
        self.assertEqual(sell.code, "STC")
        # $619.94 received against $620.00 notional = 6 cents of fees.
        self.assertAlmostEqual(sell.fees, 0.06, places=2)

    def test_ignores_cash_rows(self):
        csv_text = HEADER + (
            row("8/1/2026", "", "ACH Deposit", "ACH", "", "", "$5,000.00")
            + row("8/2/2026", "AAPL", "Apple Inc Dividend", "CDIV", "", "", "$12.40")
            + row("8/3/2026", "", "Gold Fee", "GOLD", "", "", "($5.00)")
        )
        self.assertEqual(bi.parse_activity_csv(csv_text), [])

    def test_parses_stock_rows(self):
        csv_text = HEADER + row("8/4/2026", "TSLA", "Tesla Inc", "Buy", "10", "$248.15", "($2,481.50)")
        fill, = bi.parse_activity_csv(csv_text)
        self.assertEqual(fill.instrument, "stock")
        self.assertEqual(fill.quantity, 10)
        self.assertEqual(fill.price, 248.15)
        self.assertIsNone(fill.strike)

    def test_rejects_a_file_that_is_not_an_activity_export(self):
        with self.assertRaises(bi.ImportError_) as ctx:
            bi.parse_activity_csv("date,ticker,pnl\n2026-08-01,NVDA,120\n")
        self.assertIn("Robinhood activity CSV", str(ctx.exception))

    def test_handles_puts_and_comma_strikes(self):
        csv_text = HEADER + row("8/5/2026", "SPY", "SPY 9/19/2026 Put $1,250.00", "BTO",
                                "1", "$12.05", "($1,205.00)")
        fill, = bi.parse_activity_csv(csv_text)
        self.assertEqual(fill.instrument, "put")
        self.assertEqual(fill.strike, 1250.0)


class MatchingTest(unittest.TestCase):
    def test_round_trip_becomes_one_closed_trade(self):
        trades = bi.match_fills(bi.parse_activity_csv(ROUND_TRIP))
        self.assertEqual(len(trades), 1)
        t = trades[0]
        self.assertEqual(t["direction"], "long")
        self.assertEqual(t["quantity"], 2)
        self.assertEqual(t["entry_price"], 2.35)
        self.assertEqual(t["exit_price"], 3.10)
        self.assertEqual(t["entry_date"], "2026-08-12")
        self.assertEqual(t["exit_date"], "2026-08-14")
        self.assertEqual(t["needs_review"], 0)
        # (3.10 - 2.35) * 2 * 100 = $150 gross, less 6c of fees.
        self.assertAlmostEqual(bi._pnl_of(t), 149.94, places=2)

    def test_partial_exit_splits_the_position(self):
        csv_text = HEADER + (
            row("8/12/2026", "NVDA", "NVDA 8/15/2026 Call $180.00", "BTO", "4", "$2.00", "($800.00)")
            + row("8/13/2026", "NVDA", "NVDA 8/15/2026 Call $180.00", "STC", "1", "$3.00", "$300.00")
        )
        trades = bi.match_fills(bi.parse_activity_csv(csv_text))
        closed = [t for t in trades if t["exit_price"] is not None]
        still_open = [t for t in trades if t["exit_price"] is None]
        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["quantity"], 1)
        self.assertEqual(len(still_open), 1)
        self.assertEqual(still_open[0]["quantity"], 3)

    def test_fifo_pairs_the_oldest_lot_first(self):
        csv_text = HEADER + (
            row("8/10/2026", "AMD", "AMD 9/19/2026 Call $150.00", "BTO", "1", "$1.00", "($100.00)")
            + row("8/11/2026", "AMD", "AMD 9/19/2026 Call $150.00", "BTO", "1", "$2.00", "($200.00)")
            + row("8/12/2026", "AMD", "AMD 9/19/2026 Call $150.00", "STC", "1", "$3.00", "$300.00")
        )
        trades = bi.match_fills(bi.parse_activity_csv(csv_text))
        closed = [t for t in trades if t["exit_price"] is not None]
        self.assertEqual(closed[0]["entry_price"], 1.00)      # the 8/10 lot, not the 8/11 one
        self.assertEqual(closed[0]["entry_date"], "2026-08-10")

    def test_expiration_closes_at_zero_without_a_review_flag(self):
        csv_text = HEADER + (
            row("8/12/2026", "MSFT", "MSFT 8/15/2026 Call $500.00", "BTO", "1", "$1.20", "($120.00)")
            + row("8/15/2026", "MSFT", "MSFT 8/15/2026 Call $500.00", "OEXP", "1", "", "")
        )
        t, = bi.match_fills(bi.parse_activity_csv(csv_text))
        self.assertEqual(t["exit_price"], 0.0)
        self.assertEqual(t["needs_review"], 0)
        self.assertEqual(bi._pnl_of(t), -120.0)

    def test_assignment_is_flagged_for_review(self):
        csv_text = HEADER + (
            row("8/1/2026", "F", "F 8/15/2026 Put $12.00", "STO", "1", "$0.50", "$50.00")
            + row("8/15/2026", "F", "F 8/15/2026 Put $12.00", "OASGN", "1", "", "")
        )
        t, = bi.match_fills(bi.parse_activity_csv(csv_text))
        self.assertEqual(t["direction"], "short")
        self.assertEqual(t["needs_review"], 1)
        self.assertIn("assigned", t["review_note"])
        # Short put expiring/assigned at 0 keeps the credit.
        self.assertEqual(bi._pnl_of(t), 50.0)

    def test_exit_without_an_entry_is_booked_flat_and_flagged(self):
        csv_text = HEADER + row("8/14/2026", "NVDA", "NVDA 8/15/2026 Call $180.00",
                                "STC", "2", "$3.10", "$620.00")
        t, = bi.match_fills(bi.parse_activity_csv(csv_text))
        self.assertEqual(t["needs_review"], 1)
        self.assertIn("no matching entry", t["review_note"])
        self.assertEqual(bi._pnl_of(t), 0.0)  # cannot fake a win it cannot prove

    def test_same_strike_different_expiry_do_not_cross_match(self):
        csv_text = HEADER + (
            row("8/10/2026", "NVDA", "NVDA 8/15/2026 Call $180.00", "BTO", "1", "$2.00", "($200.00)")
            + row("8/11/2026", "NVDA", "NVDA 9/19/2026 Call $180.00", "STC", "1", "$5.00", "$500.00")
        )
        trades = bi.match_fills(bi.parse_activity_csv(csv_text))
        self.assertEqual(len(trades), 2)
        self.assertTrue(any(t["needs_review"] for t in trades))
        self.assertTrue(any(t["exit_price"] is None for t in trades))


class RealExportQuirksTest(unittest.TestCase):
    """Cases discovered in the owner's actual export, so they stay fixed."""

    def test_oexp_with_prose_prefix_closes_the_position(self):
        # Real OEXP rows read "Option Expiration for PLTR 8/7/2026 Put $165.00"
        # and carry quantities like "20S".
        csv_text = HEADER + (
            row("8/7/2026", "PLTR", "PLTR 8/7/2026 Put $165.00", "BTO", "20", "$2.60", "($5,200.00)")
            + row("8/7/2026", "PLTR", "Option Expiration for PLTR 8/7/2026 Put $165.00",
                  "OEXP", "20S", "", "")
        )
        t, = bi.match_fills(bi.parse_activity_csv(csv_text))
        self.assertEqual(t["exit_price"], 0.0)
        self.assertEqual(t["needs_review"], 0)
        self.assertEqual(bi._pnl_of(t), -5200.0)

    def test_stock_short_via_ss_and_bc(self):
        csv_text = HEADER + (
            row("8/7/2026", "MSFT", "Microsoft CUSIP: 594918104", "SS", "100", "$500.90", "$50,090.00")
            + row("8/7/2026", "MSFT", "Microsoft CUSIP: 594918104", "BC", "100", "$500.43", "($50,043.00)")
        )
        t, = bi.match_fills(bi.parse_activity_csv(csv_text))
        self.assertEqual(t["direction"], "short")
        self.assertEqual(t["instrument"], "stock")
        # Short: sold at 500.90, covered at 500.43 → +$47 gross on 100 shares.
        self.assertAlmostEqual(bi._pnl_of(t), 47.0, delta=1.0)

    def test_open_option_past_expiry_is_flagged_not_guessed(self):
        csv_text = HEADER + (
            row("6/22/2026", "VUG", "VUG 7/17/2026 Call $91.00", "BTO", "1", "$0.65", "($65.00)")
            + row("8/14/2026", "AAPL", "Apple", "Buy", "1", "$310.00", "($310.00)")
        )
        trades = bi.match_fills(bi.parse_activity_csv(csv_text))
        vug = next(t for t in trades if t["ticker"] == "VUG")
        self.assertIsNone(vug["exit_price"])          # not silently closed
        self.assertEqual(vug["needs_review"], 1)
        self.assertIn("expired", vug["review_note"])


class StatementTextTest(unittest.TestCase):
    def test_parses_trade_lines_out_of_statement_text(self):
        text = (
            "ROBINHOOD SECURITIES MONTHLY STATEMENT\n"
            "Account Activity\n"
            "8/12/2026 NVDA 8/15/2026 Call $180.00 BTO 2 $2.35 ($470.00)\n"
            "8/14/2026 NVDA 8/15/2026 Call $180.00 STC 2 $3.10 $619.94\n"
            "Page 2 of 4  Portfolio Summary  Total $12,345.00\n"
        )
        fills = bi.parse_statement_text(text)
        self.assertEqual(len(fills), 2)
        self.assertEqual(fills[0].strike, 180.0)
        self.assertEqual(fills[1].code, "STC")

    def test_prose_does_not_become_trades(self):
        text = ("Your account is protected by SIPC up to $500,000.\n"
                "Interest earned this period: $3.21\n")
        self.assertEqual(bi.parse_statement_text(text), [])


class ImportToJournalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # journal.DB_PATH is module-level global state. Point it at a temp file
        # for these tests and put it back afterwards — leaving it dangling makes
        # every later test in the suite open a database inside a deleted folder.
        original_db = journal.DB_PATH
        self.addCleanup(setattr, journal, "DB_PATH", original_db)
        journal.DB_PATH = Path(self.tmp.name) / "journal.db"
        journal.init()
        self.user = journal.register("importtest", "pw123")
        self.uid = journal.user_for_token(self.user["token"])["id"]

    def test_dry_run_writes_nothing(self):
        summary = bi.import_fills(bi.parse_activity_csv(ROUND_TRIP), self.uid, dry_run=True)
        self.assertEqual(summary["new_trades"], 1)
        self.assertEqual(summary["written"], 0)
        self.assertEqual(journal.list_trades(self.uid), [])

    def test_import_lands_in_the_journal_with_net_pnl(self):
        summary = bi.import_fills(bi.parse_activity_csv(ROUND_TRIP), self.uid)
        self.assertEqual(summary["written"], 1)
        t, = journal.list_trades(self.uid)
        self.assertEqual(t["ticker"], "NVDA")
        self.assertEqual(t["status"], "closed")
        self.assertAlmostEqual(t["pnl"], 149.94, places=2)   # net of fees, not gross
        self.assertTrue(t["source"].startswith("robinhood"))

    def test_reimporting_the_same_export_is_a_no_op(self):
        bi.import_fills(bi.parse_activity_csv(ROUND_TRIP), self.uid)
        again = bi.import_fills(bi.parse_activity_csv(ROUND_TRIP), self.uid)
        self.assertEqual(again["written"], 0)
        self.assertEqual(again["skipped_duplicates"], 1)
        self.assertEqual(len(journal.list_trades(self.uid)), 1)

    def test_overlapping_export_adds_only_the_new_trade(self):
        bi.import_fills(bi.parse_activity_csv(ROUND_TRIP), self.uid)
        wider = ROUND_TRIP + row("8/20/2026", "AMD", "AMD 9/19/2026 Call $150.00",
                                 "BTO", "1", "$1.50", "($150.00)")
        summary = bi.import_fills(bi.parse_activity_csv(wider), self.uid)
        self.assertEqual(summary["written"], 1)
        self.assertEqual(summary["skipped_duplicates"], 1)
        self.assertEqual(len(journal.list_trades(self.uid)), 2)

    def test_stats_report_provenance(self):
        bi.import_fills(bi.parse_activity_csv(ROUND_TRIP), self.uid)
        journal.add_trade({"ticker": "SPY", "instrument": "call", "direction": "long",
                           "quantity": 1, "entry_price": 1.0, "entry_date": "2026-08-01",
                           "exit_price": 2.0, "exit_date": "2026-08-02"}, self.uid)
        s = journal.stats(self.uid)
        self.assertEqual(s["imported_trades"], 1)
        self.assertEqual(s["manual_trades"], 1)
        self.assertEqual(s["needs_review"], 0)

    def test_import_file_from_bytes(self):
        summary = bi.import_file(content=ROUND_TRIP.encode(), filename="activity.csv",
                                 user_id=self.uid, dry_run=True)
        self.assertEqual(summary["trades_found"], 1)
        self.assertEqual(summary["date_range"], ["2026-08-12", "2026-08-14"])

    def test_empty_file_explains_itself(self):
        with self.assertRaises(bi.ImportError_) as ctx:
            bi.import_file(content=HEADER.encode(), filename="activity.csv", user_id=self.uid)
        self.assertIn("no trade rows found", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
