"""Gate policy: which standing alerts actually justify standing down.

The original gate had one rule — any high-priority Analyst alert inside the
window blocks the run. Principled, and in practice equivalent to OFF: the
Analyst flags "macro risk in headlines" whenever two headlines share a topic,
and in a news cycle where CPI, tariffs, and the Fed are ambient weather, that
fired every day. Nineteen consecutive autonomous runs stood down on headlines
that moved nothing. A gate that never opens is not safety, it is a decorative
wall; the safety was coming from the $300 cap and the daily trade limit the
gate never let anything reach.

This module splits standing alerts into what actually predicts trouble:

- **Ticker alerts** (negative news clusters naming a symbol) — always block.
  They are rare, specific, and exactly what a trader would stop for.
- **Macro event days** — block on FOMC/CPI day and the day before, straight
  from the app's own calendar. This is the "avoid opening new positions into
  it" rule the calendar module already states.
- **Headline storms** — an ambient macro topic normally clears 2-4 headlines
  a scan. A topic clearing STORM_MIN or more in one scan is not weather, it
  is an event in progress: block.
- **Ambient macro** (everything else) — reported, never blocking.

Everything here is a pure function over rows and dates so tests can pin the
behavior without a database or a network.
"""

from __future__ import annotations

from datetime import date, timedelta

from .calendar_events import CPI_2026, FOMC_2026

# One scan's headline count at which a macro topic stops being background
# noise. Ambient chatter runs 2-4; a real event floods every feed at once.
STORM_MIN = 6

_EVENT_DAYS = {d: "FOMC" for d in FOMC_2026}
_EVENT_DAYS.update({d: "CPI" for d in CPI_2026})


def macro_event_window(today: date) -> dict | None:
    """The scheduled macro event making today a no-new-positions day, if any.

    Blocks the event day itself and the day before it — positions opened into
    a binary print are the trade the calendar module explicitly warns against.
    """
    for offset, phase in ((0, "today"), (1, "tomorrow")):
        d = (today + timedelta(days=offset)).isoformat()
        if d in _EVENT_DAYS:
            return {"kind": _EVENT_DAYS[d], "date": d, "phase": phase}
    return None


def _headline_count(row: dict) -> int:
    payload = row.get("payload") or {}
    if isinstance(payload, dict):
        try:
            return int(payload.get("count") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def classify(rows: list[dict], *, today: date, storm_min: int = STORM_MIN) -> dict:
    """Split standing alerts into blocking and ambient, with stated reasons.

    `rows` is tracking.standing_alerts() output. Fail-closed bias: an alert
    that names a ticker, or that this function cannot confidently read
    (no payload, unrecognizable shape), goes to blocking — only the one
    well-understood case (an ambient macro topic below storm size on a
    non-event day) is allowed through.
    """
    event = macro_event_window(today)
    blocking: list[dict] = []
    ambient: list[dict] = []

    for row in rows:
        why = None
        if row.get("ticker"):
            why = f"names {row['ticker']} — ticker-specific risk always blocks"
        else:
            count = _headline_count(row)
            if count >= storm_min:
                why = (f"headline storm: {count} headlines on one topic in a "
                       f"single scan (threshold {storm_min})")
            elif count == 0 and not str(row.get("subject", "")).startswith(
                    "Macro risk in headlines"):
                # Not a macro alert and not readable as one: refuse to wave
                # through mail this policy does not understand.
                why = "unrecognized high-priority alert — failing closed"

        entry = {"id": row.get("id"), "ts": row.get("ts"),
                 "subject": row.get("subject"), "ticker": row.get("ticker"),
                 "headlines": _headline_count(row)}
        if why:
            entry["why"] = why
            blocking.append(entry)
        else:
            ambient.append(entry)

    # A scheduled event day stands the run down on its own, alerts or not —
    # the calendar does not need the news to confirm the Fed exists.
    return {
        "blocking": blocking,
        "ambient": ambient,
        "event": event,
        "stand_down": bool(blocking) or event is not None,
    }
