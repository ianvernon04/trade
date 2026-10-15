"""Macro calendar tests: the dates are typed in by hand, so pin them.

The gate stands down on FOMC/CPI days straight from these lists. A typo
(2026-10-13 for a release that happens on the 14th — this really happened)
means trading INTO the print, so every date is pinned to the published
schedule and to a weekday, and the gate's exhausted-calendar stand-down is
checked around its boundaries.

Sources (verified 2026-10-11):
- https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- https://www.bls.gov/schedule/2026/home.htm
- https://www.bls.gov/schedule/2027/home.htm
"""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app import calendar_events as cal
from app import gate

# Second day of each two-day meeting, from the Fed's calendar page.
FOMC_VERIFIED = {
    2026: ["2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
           "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09"],
    2027: ["2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
           "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08"],
}
# CPI release mornings, from BLS's full-year schedule pages.
CPI_VERIFIED = {
    2026: ["2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
           "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
           "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10"],
    2027: ["2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13",
           "2027-05-12", "2027-06-10", "2027-07-14", "2027-08-11",
           "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10"],
}

LAST_FOMC = date(2027, 12, 8)
LAST_CPI = date(2027, 12, 10)


class PinnedDatesTest(unittest.TestCase):
    def test_fomc_lists_match_verified_schedule(self):
        self.assertEqual(cal.FOMC_2026, FOMC_VERIFIED[2026])
        self.assertEqual(cal.FOMC_2027, FOMC_VERIFIED[2027])

    def test_cpi_lists_match_verified_schedule(self):
        self.assertEqual(cal.CPI_2026, CPI_VERIFIED[2026])
        self.assertEqual(cal.CPI_2027, CPI_VERIFIED[2027])

    def test_union_lists_are_sorted_and_complete(self):
        self.assertEqual(cal.FOMC_DATES, sorted(FOMC_VERIFIED[2026] + FOMC_VERIFIED[2027]))
        self.assertEqual(cal.CPI_DATES, sorted(CPI_VERIFIED[2026] + CPI_VERIFIED[2027]))
        self.assertEqual(len(cal.FOMC_DATES), 16)
        self.assertEqual(len(cal.CPI_DATES), 24)

    def test_every_date_parses_and_is_a_weekday(self):
        for kind, dates in cal.SERIES.items():
            for d in dates:
                dt = date.fromisoformat(d)  # raises on a malformed string
                self.assertLess(dt.weekday(), 5, f"{kind} {d} falls on a weekend")

    def test_fomc_decision_days_are_wednesdays(self):
        # Every two-day meeting in 2026-2027 ends on a Wednesday.
        for d in cal.FOMC_DATES:
            self.assertEqual(date.fromisoformat(d).weekday(), 2, d)

    def test_the_two_corrected_dates(self):
        # The typos that prompted this file: Jan CPI (Feb 13, not 11) and
        # Sep CPI (Oct 14, not 13).
        self.assertIn("2026-02-13", cal.CPI_2026)
        self.assertNotIn("2026-02-11", cal.CPI_2026)
        self.assertIn("2026-10-14", cal.CPI_2026)
        self.assertNotIn("2026-10-13", cal.CPI_2026)

    def test_last_known_event_date(self):
        self.assertEqual(cal.last_known_event_date(), {"FOMC": LAST_FOMC, "CPI": LAST_CPI})


class ExhaustionTest(unittest.TestCase):
    def test_well_inside_calendar_is_not_exhausted(self):
        self.assertFalse(cal.calendar_exhausted(date(2026, 10, 11)))
        self.assertFalse(cal.calendar_exhausted(date(2026, 10, 11), 365))

    def test_boundary_on_the_shorter_series(self):
        # FOMC ends first (Dec 8); exhaustion is per series, so the FOMC edge
        # is the binding one even though CPI runs two days longer.
        self.assertFalse(cal.calendar_exhausted(LAST_FOMC - timedelta(days=30), 30))
        self.assertTrue(cal.calendar_exhausted(LAST_FOMC - timedelta(days=29), 30))

    def test_horizon_is_respected(self):
        day = LAST_FOMC - timedelta(days=40)
        self.assertFalse(cal.calendar_exhausted(day, 40))
        self.assertTrue(cal.calendar_exhausted(day, 41))

    def test_past_the_end_is_exhausted(self):
        self.assertTrue(cal.calendar_exhausted(date(2028, 1, 3)))
        self.assertTrue(cal.calendar_exhausted(date(2028, 1, 3), 0))


class GateFailsClosedOnExhaustionTest(unittest.TestCase):
    def test_stand_down_with_no_alerts(self):
        day = LAST_FOMC - timedelta(days=gate.CALENDAR_HORIZON_DAYS - 1)
        v = gate.classify([], today=day)
        self.assertTrue(v["stand_down"])
        self.assertEqual(
            v["calendar_exhausted"],
            "macro calendar exhausted: last known FOMC 2027-12-08, CPI 2027-12-10 "
            "— add next year's dates")
        self.assertEqual(len(v["blocking"]), 1)
        self.assertIn("exhausted", v["blocking"][0]["why"])
        self.assertIsInstance(v["blocking"][0]["ts"], str)  # the CLI slices it

    def test_stand_down_regardless_of_ambient_alerts(self):
        row = {"id": 1, "ts": "2027-11-10T08:00:00Z", "ticker": None, "priority": "high",
               "subject": "Macro risk in headlines: inflation",
               "payload": {"topic": "inflation", "count": 2}, "read_at": None}
        v = gate.classify([row], today=date(2027, 11, 10))
        self.assertTrue(v["stand_down"])
        self.assertEqual(len(v["ambient"]), 1)  # still reported as ambient
        self.assertIsNotNone(v["calendar_exhausted"])

    def test_inside_calendar_is_unaffected(self):
        v = gate.classify([], today=date(2026, 10, 12))
        self.assertFalse(v["stand_down"])
        self.assertIsNone(v["calendar_exhausted"])

    def test_gate_sees_2027_event_days(self):
        self.assertEqual(gate.macro_event_window(date(2027, 1, 27))["kind"], "FOMC")
        self.assertEqual(gate.macro_event_window(date(2027, 2, 10)),
                         {"kind": "CPI", "date": "2027-02-11", "phase": "tomorrow"})


class NonMacroAlertsFailClosedTest(unittest.TestCase):
    """M6: only the Analyst's macro subject may ride the ambient path."""

    QUIET_DAY = date(2026, 8, 20)

    def _alert(self, subject, count):
        return {"id": 7, "ts": "2026-08-20T09:00:00Z", "ticker": None, "priority": "high",
                "subject": subject, "payload": {"count": count}, "read_at": None}

    def test_small_non_macro_alert_blocks(self):
        # Before: a readable count below storm size waved anything through.
        v = gate.classify([self._alert("Portfolio delta exceeds limit", 2)],
                          today=self.QUIET_DAY)
        self.assertTrue(v["stand_down"])
        self.assertIn("failing closed", v["blocking"][0]["why"])
        self.assertEqual(v["ambient"], [])

    def test_macro_prefix_still_ambient(self):
        v = gate.classify([self._alert(f"{gate.MACRO_SUBJECT_PREFIX}: tariffs/trade", 2)],
                          today=self.QUIET_DAY)
        self.assertFalse(v["stand_down"])
        self.assertEqual(len(v["ambient"]), 1)

    def test_prefix_matches_newsagent(self):
        # The gate's whitelist must track the Analyst's actual subject line.
        import inspect
        from app import newsagent
        self.assertIn(f'"{gate.MACRO_SUBJECT_PREFIX}: ', inspect.getsource(newsagent))


if __name__ == "__main__":
    unittest.main()
