"""Regression tests for compose_messages' two action shapes.

analyze_positions emits two different dicts: specific actions carrying a
"type" (take_profit / reversal), and generic monitoring rows that carry only
"alerts". compose_messages indexed act["type"] directly, which raised
KeyError on the generic shape.

That path was unreachable for as long as the agents read the (empty) journal,
and crashed the first time a real broker position produced a plain expiry or
earnings warning — an ZZTOP earnings notice, in practice. Reconstruction of
the book itself is covered by tests/test_portfolio.py; this file only pins
the message-composition contract that broke.
"""

from __future__ import annotations

import unittest

from app import positionagent


def _analysis(**over) -> dict:
    base = {
        "n_positions": 1, "theta_sum": 0.0, "unpriced": 0, "unpriced_tickers": [],
        "actions": [], "dte_warns": [], "earnings_warns": [],
        "blind": False, "stale": False, "source": "test", "age_hours": 0,
    }
    base.update(over)
    return base


class TestActionShapes(unittest.TestCase):
    def test_generic_action_without_type_does_not_crash(self):
        a = _analysis(
            actions=[{"ticker": "ZZTOP", "instrument": "stock", "position_id": "rh:ZZTOP",
                      "strike": None, "expiry": None, "unrealized_pnl_pct": 3.2,
                      "alerts": ["earnings 5d away"]}],
            earnings_warns=[("ZZTOP", 5, {})],
        )
        msgs = positionagent.compose_messages(a)
        self.assertTrue(any("arnings" in m["subject"] for m in msgs))

    def test_typed_actions_still_produce_their_messages(self):
        a = _analysis(
            n_positions=2,
            actions=[
                {"type": "take_profit", "ticker": "NVDA", "position_id": "1",
                 "pnl_pct": 80.0, "note": "call up"},
                {"type": "reversal", "ticker": "MU", "position_id": "2",
                 "pnl_pct": -60.0, "note": "call down"},
            ],
        )
        msgs = positionagent.compose_messages(a)
        subjects = " | ".join(m["subject"] for m in msgs)
        self.assertIn("NVDA", subjects)
        self.assertIn("MU", subjects)
        # The reversal is the one that has to reach the Trader loudly.
        high = [m for m in msgs if m["priority"] == "high"]
        self.assertEqual([m.get("ticker") for m in high], ["MU"])

    def test_mixed_typed_and_generic_actions_coexist(self):
        a = _analysis(
            n_positions=2,
            actions=[
                {"type": "take_profit", "ticker": "NVDA", "position_id": "1",
                 "pnl_pct": 80.0, "note": "call up"},
                {"ticker": "ZZTOP", "instrument": "stock", "position_id": "2",
                 "strike": None, "expiry": None, "unrealized_pnl_pct": 3.2,
                 "alerts": ["earnings 5d away"]},
            ],
        )
        msgs = positionagent.compose_messages(a)  # must not raise
        self.assertTrue(any("NVDA" in m["subject"] for m in msgs))


if __name__ == "__main__":
    unittest.main()
