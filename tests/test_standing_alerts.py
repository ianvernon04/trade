"""The macro-risk gate must key on the risk window, not on the read flag.

Routine step 2 blocks new positions when a high-priority Analyst alert
landed inside a 12-hour window. It was implemented by reading the inbox
with ``--unread``, which answers a different question. The two agree only
until something marks the mail read — and the routine itself marks it read
on every run. The second run of the day then saw an empty inbox and would
have traded through macro risk the first run correctly refused.
"""

from __future__ import annotations

import unittest

from app import tracking

from test_tracking import TrackingTestCase  # sqlite-in-a-tempdir harness


class TestStandingAlerts(TrackingTestCase):
    def _alert(self, subject, *, frm="analyst", priority="high", ts=None):
        return tracking.send_message(frm, "trader", subject,
                                     priority=priority, kind="alert", ts=ts)

    def test_read_alerts_still_count(self):
        """The whole point: marking a warning read does not end the risk."""
        self._alert("Macro risk in headlines: tariffs/trade")
        tracking.mark_read("trader")
        self.assertEqual(tracking.inbox("trader", unread_only=True), [],
                         "precondition: nothing unread")
        standing = tracking.standing_alerts("trader", hours=12)
        self.assertEqual(len(standing), 1)
        self.assertIsNotNone(standing[0]["read_at"], "and it is indeed read")

    def test_alerts_outside_the_window_do_not_count(self):
        self._alert("Old news", ts="2026-08-05T00:00:00Z")
        self.assertEqual(
            tracking.standing_alerts("trader", hours=12,
                                     now="2026-08-06T12:00:00Z"), [])

    def test_alert_inside_the_window_counts(self):
        self._alert("Fresh macro risk", ts="2026-08-06T06:00:00Z")
        self.assertEqual(
            len(tracking.standing_alerts("trader", hours=12,
                                         now="2026-08-06T12:00:00Z")), 1)

    def test_boundary_is_inclusive(self):
        self._alert("Exactly on the edge", ts="2026-08-06T00:00:00Z")
        self.assertEqual(
            len(tracking.standing_alerts("trader", hours=12,
                                         now="2026-08-06T12:00:00Z")), 1)

    def test_normal_priority_is_not_a_block(self):
        self._alert("Routine digest", priority="normal")
        self.assertEqual(tracking.standing_alerts("trader", hours=12), [])

    def test_can_scope_to_one_sender(self):
        """Step 2 is about the Analyst; a blind-Risk-Manager alert is not macro news."""
        self._alert("Macro risk", frm="analyst")
        self._alert("Risk Manager is blind", frm="risk_manager")
        self.assertEqual(len(tracking.standing_alerts("trader", hours=12)), 2)
        scoped = tracking.standing_alerts("trader", hours=12, from_agent="analyst")
        self.assertEqual([m["from_agent"] for m in scoped], ["analyst"])

    def test_other_agents_inboxes_are_not_consulted(self):
        tracking.send_message("analyst", "someone_else", "Not for the trader",
                              priority="high", kind="alert")
        self.assertEqual(tracking.standing_alerts("trader", hours=12), [])

    def test_zero_hours_is_an_empty_window_not_unbounded(self):
        """A zero window must block nothing rather than match everything."""
        self._alert("Fresh", ts="2026-08-06T11:59:00Z")
        self.assertEqual(
            tracking.standing_alerts("trader", hours=0,
                                     now="2026-08-06T12:00:00Z"), [])


if __name__ == "__main__":
    unittest.main()
