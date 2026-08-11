"""Daily trend read, and the rule that holdings never travel with the code.

The privacy test is not decoration. This repo is public and deploys from
GitHub; the owner's positions had to be scrubbed out of git history once
already. A tab called "the stocks you trade" is exactly the feature that
would put them back, so the fallback is pinned here.
"""

from __future__ import annotations

import unittest

import numpy as np
import pandas as pd

from app import trend

from test_tracking import TrackingTestCase


def _frame(closes: list[float]) -> pd.DataFrame:
    """OHLCV with a modest daily range, in the lowercase schema data.py uses."""
    c = pd.Series(closes, dtype=float)
    return pd.DataFrame({
        "open": c.shift(1).fillna(c.iloc[0]),
        "high": c * 1.01,
        "low": c * 0.99,
        "close": c,
        "volume": pd.Series([1_000_000] * len(c), dtype=float),
    })


class TestTrendMath(unittest.TestCase):
    def _run(self, closes):
        orig = trend.data.get_history
        trend.data.get_history = lambda *a, **k: _frame(closes)
        try:
            return trend.daily_trend("TEST")
        finally:
            trend.data.get_history = orig

    def test_steady_climb_reads_up(self):
        r = self._run([100 + i for i in range(60)])
        self.assertEqual(r["direction"], "up")
        self.assertGreater(r["slope_atr"], 0)
        self.assertFalse(r["conflict"])

    def test_steady_slide_reads_down(self):
        r = self._run([200 - i for i in range(60)])
        self.assertEqual(r["direction"], "down")
        self.assertLess(r["slope_atr"], 0)

    def test_noise_around_a_level_reads_sideways(self):
        closes = [100 + (1 if i % 2 else -1) * 0.4 for i in range(60)]
        self.assertEqual(self._run(closes)["direction"], "sideways")

    def test_conflicting_slope_and_net_move_is_not_called_a_direction(self):
        """A slide that recovers can fit a rising line over a net loss.

        Reporting that as "up" next to a negative 20-day return is how a
        dashboard talks someone into a position. Unresolved is the honest read.
        """
        # The realistic shape: a gap down (earnings, say) just outside the
        # 20-day window, then a steady climb inside it that never regains the
        # old level. Every day in the window trends up; the position is still
        # down over the period.
        closes = [100.0] * 25 + [70 + 1.3 * i for i in range(20)]
        r = self._run(closes)
        self.assertLess(r["change_20d"], 0, "net move over the window is negative")
        self.assertGreater(r["slope_atr"], 0, "but the fitted slope is positive")
        self.assertTrue(r["conflict"])
        self.assertEqual(r["direction"], "sideways")
        self.assertEqual(r["strength"], "weak")

    def test_streak_counts_consecutive_days_and_signs_them(self):
        r = self._run([100] * 40 + [101, 102, 103])
        self.assertEqual(r["streak"], 3)
        r = self._run([100] * 40 + [99, 98])
        self.assertEqual(r["streak"], -2)

    def test_slope_is_scale_free(self):
        """A $15 name and a $500 name trending alike should score alike."""
        cheap = self._run([15 + i * 0.15 for i in range(60)])
        rich = self._run([500 + i * 5.0 for i in range(60)])
        self.assertAlmostEqual(cheap["slope_atr"], rich["slope_atr"], places=1)

    def test_short_history_is_reported_not_guessed(self):
        r = self._run([100, 101, 102])
        self.assertIn("error", r)

    def test_a_broken_ticker_does_not_raise(self):
        orig = trend.data.get_history
        trend.data.get_history = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("delisted"))
        try:
            r = trend.daily_trend("BAD")
        finally:
            trend.data.get_history = orig
        self.assertIn("delisted", r["error"])


class TestUniverseNeverLeaksHoldings(TrackingTestCase):
    def test_no_tracking_data_falls_back_to_the_public_watchlist(self):
        u = trend.universe()
        self.assertEqual(u["source"], "watchlist")
        self.assertEqual(u["tickers"], list(trend.data.DEFAULT_WATCHLIST))

    def test_local_holdings_are_used_when_present(self):
        trend.tracking.log_event("position", ticker="ZZTOP", source="robinhood-mcp",
                                 payload={"symbol": "ZZTOP", "quantity": "10"})
        u = trend.universe()
        self.assertEqual(u["source"], "account")
        self.assertIn("ZZTOP", u["tickers"])

    def test_source_is_reported_so_the_ui_can_say_which_it_is(self):
        """A table headed "stocks you trade" must admit when it is not those."""
        self.assertEqual(trend.universe()["source"], "watchlist")


if __name__ == "__main__":
    unittest.main()
