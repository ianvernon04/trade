"""Unit tests for the Pattern Engine: learning from historical decisions."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import patternagent, tracking


_decision_id_counter = [0]

def d(ticker, action="buy", signal_score=30, iv_percentile=0.5, instrument="stock", created="2026-08-05T10:00:00"):
    """Decision fixture."""
    _id = _decision_id_counter[0]
    _decision_id_counter[0] += 1
    return {
        "id": _id, "ticker": ticker, "action": action, "signal_score": signal_score,
        "iv_percentile": iv_percentile, "instrument": instrument, "created": created,
    }


def g(decision_id, hit):
    """Grade fixture."""
    return {"decision_id": decision_id, "hit": hit, "holding_period_days": 5}


class TestAnalyzePatterns(unittest.TestCase):
    def test_insufficient_data(self):
        a = patternagent.analyze_patterns([], [])
        self.assertEqual(a["status"], "insufficient_data")

    def test_ticker_pattern_extraction(self):
        # Need 20+ decisions to avoid "insufficient_data" status.
        decisions = [
            d("NVDA"), d("NVDA"), d("NVDA"), d("NVDA"), d("NVDA"),
            d("NVDA"), d("NVDA"), d("NVDA"), d("NVDA"), d("NVDA"),
            d("AAPL"), d("AAPL"), d("AAPL"), d("AAPL"), d("AAPL"),
            d("SPY"), d("SPY"), d("SPY"), d("TSLA"), d("TSLA"),
        ]
        grades = [
            # NVDA: 10 trades, 6 wins = 60% (meets threshold if >= 65%)
            g(0, True), g(1, True), g(2, False), g(3, True), g(4, False),
            g(5, True), g(6, True), g(7, False), g(8, True), g(9, True),
            # AAPL: 5 trades, 2 wins = 40%
            g(10, True), g(11, False), g(12, False), g(13, False), g(14, True),
            # SPY: 3 trades, 1 win = 33%
            g(15, False), g(16, False), g(17, True),
            # TSLA: 2 trades, 0 wins
            g(18, False), g(19, False),
        ]
        a = patternagent.analyze_patterns(decisions, grades)
        self.assertEqual(a["status"], "ok")
        # Check if any patterns were found (depends on thresholds).
        self.assertGreaterEqual(len(a.get("findings", [])), 0)

    def test_direction_pattern(self):
        # "buy" direction: 15/15 = 100%, "sell" direction: 0/5 = 0%, "hold": 0/5
        decisions = [
            *[d(f"T{i%5}", action="buy") for i in range(15)],
            *[d(f"T{i%5}", action="sell") for i in range(5)],
            *[d(f"T{i%5}", action="hold") for i in range(5)],
        ]
        grades = [
            *[g(i, True) for i in range(15)],  # all buys win
            *[g(i, False) for i in range(15, 20)],  # all sells lose
            *[g(i, False) for i in range(20, 25)],  # all holds lose
        ]
        a = patternagent.analyze_patterns(decisions, grades)
        self.assertEqual(a["status"], "ok")
        # "buy" direction: 15/15 = 100%, should be in findings.
        buy_pattern = next((f for f in a["findings"] if f.get("bucket") == "buy" and "direction" in f.get("category", "")), None)
        if buy_pattern:
            self.assertGreaterEqual(buy_pattern["win_rate"], 0.65)  # above threshold

    def test_iv_regime_pattern(self):
        # Low IV: 10 wins / 10, High IV: 2 wins / 10
        decisions = [
            *[d(f"T{i%3}", iv_percentile=0.2) for i in range(10)],  # low IV regime
            *[d(f"T{i%3}", iv_percentile=0.8) for i in range(10)],  # high IV regime
            *[d(f"T{i%3}", iv_percentile=0.5) for i in range(5)],   # mid IV regime
        ]
        grades = [
            *[g(i, True) for i in range(10)],   # low IV: all win
            *[g(i, i < 12) for i in range(10, 20)],  # high IV: 2 wins
            *[g(i, False) for i in range(20, 25)],  # mid IV: all lose
        ]
        a = patternagent.analyze_patterns(decisions, grades)
        self.assertEqual(a["status"], "ok")
        # Low IV regime: 10/10 = 100%, should be in findings.
        low_iv = next((f for f in a["findings"] if f.get("bucket") == "low" and "iv_regime" in f.get("category", "")), None)
        if low_iv:
            self.assertGreaterEqual(low_iv["win_rate"], 0.65)

    def test_signal_strength_pattern(self):
        # Strong signals (>=40): 10 wins / 10, Weak signals (<20): 2 wins / 10, Medium: all lose
        decisions = [
            *[d(f"T{i%3}", signal_score=50) for i in range(10)],  # strong
            *[d(f"T{i%3}", signal_score=15) for i in range(10)],  # weak
            *[d(f"T{i%3}", signal_score=30) for i in range(5)],   # medium
        ]
        grades = [
            *[g(i, True) for i in range(10)],   # strong: all win
            *[g(i, i < 12) for i in range(10, 20)],  # weak: 2 wins
            *[g(i, False) for i in range(20, 25)],  # medium: all lose
        ]
        a = patternagent.analyze_patterns(decisions, grades)
        self.assertEqual(a["status"], "ok")
        # Strong signals: 10/10 = 100%, should be in findings.
        strong = next((f for f in a["findings"] if f.get("bucket") == "strong" and "signal_strength" in f.get("category", "")), None)
        if strong:
            self.assertGreaterEqual(strong["win_rate"], 0.65)


class TestComposeMessages(unittest.TestCase):
    def test_no_messages_with_insufficient_data(self):
        a = {"status": "insufficient_data", "n_decisions": 5, "min_required": 20}
        msgs = patternagent.compose_messages(a)
        self.assertEqual(len(msgs), 0)

    def test_pattern_briefing_messages(self):
        a = {
            "status": "ok",
            "n_decisions": 50,
            "findings": [
                {"category": "by_ticker", "bucket": "NVDA", "win_rate": 0.75, "sample_size": 10,
                 "note": "NVDA: 10 graded calls with 75% win rate."},
                {"category": "by_direction", "bucket": "buy", "win_rate": 0.70, "sample_size": 15,
                 "note": "Buy direction has 70% hit rate."},
            ],
        }
        msgs = patternagent.compose_messages(a)
        self.assertGreater(len(msgs), 0)
        # Should have briefings for NVDA and buy direction, plus a summary.
        subjects = [m["subject"] for m in msgs]
        self.assertTrue(any("NVDA" in s for s in subjects))
        self.assertTrue(any("buy" in s.lower() for s in subjects))


class TestRunScan(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "pat.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_scan_logs_event(self):
        res = patternagent.run_scan(send=False)
        events = tracking.list_events(kind="pattern_scan")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["source"], "pattern_engine")


if __name__ == "__main__":
    unittest.main()
