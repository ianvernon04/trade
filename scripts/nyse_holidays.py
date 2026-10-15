#!/usr/bin/env python3
"""Is the NYSE open on a given date?  Used by autotrade.sh to skip holidays.

    python3 scripts/nyse_holidays.py            # today (America/Chicago)
    python3 scripts/nyse_holidays.py 2026-11-26 # a specific date

Exit codes (the shell script branches on these):
    0  trading day
    1  market closed — weekend or full-day holiday; the reason is printed
    2  date is outside the years this file knows about; the caller should
       WARN and treat it as a trading day rather than silently never trade
       again once the list goes stale

Static list on purpose: no network, no third-party package, nothing that can
fail at 9 AM. Extend it each December. Sources (both checked 2026-10-11 and
they agree):
  https://www.nyse.com/markets/hours-calendars
  https://www.businesswire.com/news/home/20251223981478/en/NYSE-Group-Announces-2026-2027-and-2028-Holiday-and-Early-Closings-Calendar

Early-close days (13:00 ET: 2026-11-27, 2026-12-24, 2027-11-26) are NOT
listed here — the market is open, the routine runs, and a 9 AM CT run is
done long before the close.
"""
import sys
from datetime import date, datetime, timedelta, timezone

HOLIDAYS = {
    # 2026
    date(2026, 1, 1): "New Year's Day",
    date(2026, 1, 19): "Martin Luther King, Jr. Day",
    date(2026, 2, 16): "Washington's Birthday",
    date(2026, 4, 3): "Good Friday",
    date(2026, 5, 25): "Memorial Day",
    date(2026, 6, 19): "Juneteenth",
    date(2026, 7, 3): "Independence Day (observed)",
    date(2026, 9, 7): "Labor Day",
    date(2026, 11, 26): "Thanksgiving Day",
    date(2026, 12, 25): "Christmas Day",
    # 2027
    date(2027, 1, 1): "New Year's Day",
    date(2027, 1, 18): "Martin Luther King, Jr. Day",
    date(2027, 2, 15): "Washington's Birthday",
    date(2027, 3, 26): "Good Friday",
    date(2027, 5, 31): "Memorial Day",
    date(2027, 6, 18): "Juneteenth (observed)",
    date(2027, 7, 5): "Independence Day (observed)",
    date(2027, 9, 6): "Labor Day",
    date(2027, 11, 25): "Thanksgiving Day",
    date(2027, 12, 24): "Christmas Day (observed)",
}
KNOWN_YEARS = {d.year for d in HOLIDAYS}


def central_today() -> date:
    """Today in America/Chicago without depending on the system zone.

    zoneinfo is stdlib on 3.9+; macOS system python ships tzdata. If it is
    somehow missing, fall back to a fixed CST/CDT guess that is right for
    every 9 AM run (the only time this is called in practice).
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/Chicago")).date()
    except Exception:  # pragma: no cover - tzdata missing
        return (datetime.now(timezone.utc) - timedelta(hours=6)).date()


def status(d: date):
    """Return (exit_code, message) for date d."""
    if d.weekday() >= 5:
        return 1, f"{d} is a {d.strftime('%A')}"
    if d in HOLIDAYS:
        return 1, f"{d} is {HOLIDAYS[d]} (NYSE closed)"
    if d.year not in KNOWN_YEARS:
        return 2, (f"{d.year} is not in scripts/nyse_holidays.py — "
                   f"add that year's NYSE calendar")
    return 0, f"{d} is a trading day"


def main(argv):
    if len(argv) > 1:
        try:
            d = date.fromisoformat(argv[1])
        except ValueError:
            print(f"usage: {argv[0]} [YYYY-MM-DD]", file=sys.stderr)
            return 2
    else:
        d = central_today()
    code, msg = status(d)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv))
