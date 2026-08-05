"""Unit tests for the Analyst: pure analysis + the scan/mail cycle."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import newsagent, tracking


def h(title, sentiment="neutral", tickers=None):
    return {"title": title, "sentiment": sentiment, "tickers": tickers or []}


NEG_NVDA = [
    h("Nvidia shares plunge on export probe", "negative", ["NVDA"]),
    h("NVDA tumbles as rivals gain", "negative", ["NVDA"]),
    h("Nvidia faces lawsuit over chips", "negative", ["NVDA"]),
]
POS_AAPL = [
    h("Apple surges to record on strong iPhone growth", "positive", ["AAPL"]),
    h("Apple beats expectations", "positive", ["AAPL"]),
    h("Apple upgraded by analysts", "positive", ["AAPL"]),
]
MACRO = [
    h("Fed signals rate cuts could come soon"),
    h("Powell testimony rattles markets"),
]


class TestAnalyze(unittest.TestCase):
    def test_ticker_aggregation_and_macro(self):
        a = newsagent.analyze(NEG_NVDA + POS_AAPL + MACRO)
        self.assertEqual(a["n_headlines"], 8)
        self.assertEqual(a["tickers"]["NVDA"]["negative"], 3)
        self.assertEqual(a["tickers"]["NVDA"]["net"], -3)
        self.assertEqual(a["tickers"]["AAPL"]["positive"], 3)
        topics = [m["topic"] for m in a["macro"]]
        self.assertIn("fed/rates", topics)
        fed = next(m for m in a["macro"] if m["topic"] == "fed/rates")
        self.assertEqual(fed["count"], 2)

    def test_overall_tone_thresholds(self):
        bearish = newsagent.analyze([h(f"t{i}", "negative") for i in range(8)])
        self.assertEqual(bearish["overall"]["sentiment"], "bearish")
        bullish = newsagent.analyze([h(f"t{i}", "positive") for i in range(8)])
        self.assertEqual(bullish["overall"]["sentiment"], "bullish")
        thin = newsagent.analyze([h("a", "positive"), h("b", "negative")])
        self.assertEqual(thin["overall"]["sentiment"], "mixed")

    def test_compose_cluster_and_digest(self):
        msgs = newsagent.compose_messages(newsagent.analyze(NEG_NVDA + POS_AAPL + MACRO))
        subjects = [m["subject"] for m in msgs]
        self.assertIn("Negative news cluster: NVDA", subjects)
        self.assertIn("Positive news cluster: AAPL", subjects)
        self.assertIn("Macro risk in headlines: fed/rates", subjects)
        self.assertTrue(any(s.startswith("Market news digest") for s in subjects))
        nvda = next(m for m in msgs if m["subject"] == "Negative news cluster: NVDA")
        self.assertEqual(nvda["priority"], "high")
        self.assertEqual(nvda["ticker"], "NVDA")


class TestRunScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "n.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_scan_logs_event_and_delivers_mail(self):
        res = newsagent.run_scan(fetch=lambda n: NEG_NVDA + MACRO)
        self.assertGreaterEqual(len(res["delivered"]), 2)
        events = tracking.list_events(kind="news_scan")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "analyst")
        self.assertEqual(events[0]["payload"]["n_headlines"], 5)
        unread = tracking.inbox("trader", unread_only=True)
        self.assertTrue(any(m["priority"] == "high" for m in unread))

    def test_repeat_scan_dedupes_instead_of_flooding(self):
        first = newsagent.run_scan(fetch=lambda n: NEG_NVDA + MACRO)
        second = newsagent.run_scan(fetch=lambda n: NEG_NVDA + MACRO)
        self.assertGreaterEqual(len(first["delivered"]), 2)
        self.assertEqual(len(second["delivered"]), 0)
        self.assertGreaterEqual(second["deduped"], 2)
        # Inbox holds one copy of each, not two.
        subjects = [m["subject"] for m in tracking.inbox("trader")]
        self.assertEqual(len(subjects), len(set(subjects)))

    def test_no_send_mode(self):
        res = newsagent.run_scan(fetch=lambda n: NEG_NVDA, send=False)
        self.assertEqual(res["delivered"], [])
        self.assertEqual(tracking.unread_count("trader"), 0)
        self.assertEqual(len(tracking.list_events(kind="news_scan")), 1)


if __name__ == "__main__":
    unittest.main()
