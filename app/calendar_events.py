"""Macro calendar: FOMC decision days and CPI release dates.

These are the two scheduled events that move tech stocks (and IV) hardest.
Dates are from the published 2026 schedules; they occasionally shift, so the
UI labels them approximate — verify at federalreserve.gov / bls.gov.
"""

from __future__ import annotations

from datetime import date

FOMC_2026 = [  # decision (second) day of each 2026 meeting
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-17",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
]
CPI_2026 = [  # monthly CPI release mornings
    "2026-01-13", "2026-02-11", "2026-03-11", "2026-04-10",
    "2026-05-12", "2026-06-10", "2026-07-14", "2026-08-12",
    "2026-09-11", "2026-10-13", "2026-11-10", "2026-12-10",
]


def upcoming(days_ahead: int = 45) -> list[dict]:
    today = date.today()
    out = []
    for kind, dates, why in (
        ("FOMC", FOMC_2026, "Fed rate decision — volatility spike; avoid opening new positions into it"),
        ("CPI", CPI_2026, "Inflation print (8:30am ET) — frequent gap moves in tech"),
    ):
        for d in dates:
            dt = date.fromisoformat(d)
            delta = (dt - today).days
            if 0 <= delta <= days_ahead:
                out.append({"date": d, "in_days": delta, "kind": kind, "why": why})
    out.sort(key=lambda x: x["in_days"])
    return out
