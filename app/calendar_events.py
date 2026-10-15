"""Macro calendar: FOMC decision days and CPI release dates.

These are the two scheduled events that move tech stocks (and IV) hardest.
Dates are from the published schedules; they occasionally shift, so the UI
labels them approximate — verify at federalreserve.gov / bls.gov.

The calendar is a hard-coded list, so it silently runs out. The gate treats
an exhausted calendar as a reason to stand down (`calendar_exhausted`): a run
that cannot see the next FOMC or CPI date must not assume there isn't one.
When adding a year, add BOTH series — each is checked on its own.

Sources (verified 2026-10-11):
- FOMC: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
  (decision day = second day of each two-day meeting)
- CPI:  https://www.bls.gov/schedule/2026/home.htm and
        https://www.bls.gov/schedule/2027/home.htm (8:30am ET releases)
"""

from __future__ import annotations

from datetime import date, timedelta

FOMC_2026 = [  # decision (second) day of each 2026 meeting
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
FOMC_2027 = [  # decision (second) day of each 2027 meeting
    "2027-01-27", "2027-03-17", "2027-04-28", "2027-06-09",
    "2027-07-28", "2027-09-15", "2027-10-27", "2027-12-08",
]
CPI_2026 = [  # monthly CPI release mornings
    "2026-01-13", "2026-02-13", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-14", "2026-11-10", "2026-12-10",
]
CPI_2027 = [  # monthly CPI release mornings
    "2027-01-13", "2027-02-11", "2027-03-10", "2027-04-13",
    "2027-05-12", "2027-06-10", "2027-07-14", "2027-08-11",
    "2027-09-14", "2027-10-14", "2027-11-10", "2027-12-10",
]

# Every known year, one sorted list per series. Everything below reads from
# these, so a new year is a two-list addition above and nothing else.
FOMC_DATES = sorted(FOMC_2026 + FOMC_2027)
CPI_DATES = sorted(CPI_2026 + CPI_2027)

SERIES = {"FOMC": FOMC_DATES, "CPI": CPI_DATES}


def last_known_event_date() -> dict[str, date]:
    """The final scheduled date in each series — where the calendar ends."""
    return {kind: date.fromisoformat(dates[-1]) for kind, dates in SERIES.items()}


def calendar_exhausted(day: date, horizon_days: int = 30) -> bool:
    """True when `day + horizon_days` runs past the end of EITHER series.

    Checked per series, not on the union: FOMC dates reaching into next year
    say nothing about whether next year's CPI dates have been entered.
    """
    edge = day + timedelta(days=horizon_days)
    return any(edge > last for last in last_known_event_date().values())


def upcoming(days_ahead: int = 45) -> list[dict]:
    today = date.today()
    out = []
    for kind, dates, why in (
        ("FOMC", FOMC_DATES, "Fed rate decision — volatility spike; avoid opening new positions into it"),
        ("CPI", CPI_DATES, "Inflation print (8:30am ET) — frequent gap moves in tech"),
    ):
        for d in dates:
            dt = date.fromisoformat(d)
            delta = (dt - today).days
            if 0 <= delta <= days_ahead:
                out.append({"date": d, "in_days": delta, "kind": kind, "why": why})
    out.sort(key=lambda x: x["in_days"])
    return out
