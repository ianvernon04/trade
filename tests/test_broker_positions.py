"""Regression tests for compose_messages' two action shapes.

analyze_positions emits two different dicts: specific actions carrying a
"type" (take_profit / reversal), and generic monitoring rows that carry only
"alerts". compose_messages indexed act["type"] directly, which raised
KeyError on the generic shape.

That path was unreachable for as long as the agents read the (empty) journal,
and crashed the first time a real broker position produced a plain expiry or
earnings warning. Reconstruction of the book itself is covered by
tests/test_portfolio.py; this file only pins the message-composition
contract that broke.

Fixtures use placeholder tickers on purpose: this repo is public, and real
symbols from the owner's account would disclose holdings to anyone reading
the tests.
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


class TestPositionsWithoutADatabaseId(unittest.TestCase):
    """Broker-sourced positions have no "id" — there is no row to have one.

    analyze_positions indexed p["id"], which every test fixture supplied and
    the real account did not. Passing against fixtures while crashing against
    production is the failure mode worth pinning here.
    """

    BROKER_POSITION = {  # exactly the keys app/portfolio.py produces
        "ticker": "ZZTOP", "instrument": "stock", "direction": "long",
        "quantity": 10.0, "strike": None, "expiry": None, "entry_price": 50.0,
        "mark": 51.0, "unrealized_pnl": 10.0, "unrealized_pnl_pct": 2.0,
        "delta_shares": 10.0, "theta_per_day": 0.0, "iv": None, "dte": None,
        "earnings_in_days": 5, "flags": [], "underlying_price": 51.0,
        "as_of": "2026-08-06T00:00:00Z",
    }

    def test_analyze_does_not_require_an_id(self):
        a = positionagent.analyze_positions([self.BROKER_POSITION])
        self.assertEqual(a["n_positions"], 1)
        self.assertEqual(len(a["actions"]), 1)

    def test_position_id_falls_back_to_the_contract(self):
        pid = positionagent._position_id(self.BROKER_POSITION)
        self.assertIn("ZZTOP", pid)

    def test_option_id_includes_strike_and_expiry(self):
        pid = positionagent._position_id(
            {"ticker": "NVDA", "instrument": "call", "strike": 212.5,
             "expiry": "2026-09-18"})
        self.assertIn("212.5", pid)
        self.assertIn("2026-09-18", pid)

    def test_journal_id_still_wins_when_present(self):
        pid = positionagent._position_id({**self.BROKER_POSITION, "id": 42})
        self.assertEqual(pid, "42")


class TestActionShapes(unittest.TestCase):
    def test_generic_action_without_type_does_not_crash(self):
        a = _analysis(
            actions=[{"ticker": "ZZTOP", "instrument": "stock", "position_id": "rh:ZZTOP",
                      "strike": None, "expiry": None, "unrealized_pnl_pct": 2.0,
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
                 "strike": None, "expiry": None, "unrealized_pnl_pct": 2.0,
                 "alerts": ["earnings 5d away"]},
            ],
        )
        msgs = positionagent.compose_messages(a)  # must not raise
        self.assertTrue(any("NVDA" in m["subject"] for m in msgs))


if __name__ == "__main__":
    unittest.main()
