"""Unit tests for the inter-agent message bus."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app import tracking


class MessagesTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "m.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_send_inbox_roundtrip(self):
        tracking.send_message("analyst", "trader", "Digest", body="calm day",
                              payload={"n": 40})
        tracking.send_message("analyst", "trader", "Macro risk: fed/rates",
                              kind="alert", priority="high")
        msgs = tracking.inbox("trader")
        self.assertEqual(len(msgs), 2)
        # High priority sorts first regardless of send order.
        self.assertEqual(msgs[0]["subject"], "Macro risk: fed/rates")
        self.assertEqual(msgs[1]["payload"], {"n": 40})
        self.assertEqual(tracking.unread_count("trader"), 2)
        self.assertEqual(tracking.inbox("analyst"), [])

    def test_mark_read_specific_and_all(self):
        a = tracking.send_message("analyst", "trader", "one")
        tracking.send_message("analyst", "trader", "two")
        self.assertEqual(tracking.mark_read("trader", [a["id"]]), 1)
        self.assertEqual(tracking.unread_count("trader"), 1)
        self.assertEqual(len(tracking.inbox("trader", unread_only=True)), 1)
        self.assertEqual(tracking.mark_read("trader"), 1)
        self.assertEqual(tracking.unread_count("trader"), 0)

    def test_dedupe_window(self):
        first = tracking.send_message("analyst", "trader", "Negative news cluster: NVDA",
                                      kind="alert", dedupe_hours=12)
        again = tracking.send_message("analyst", "trader", "Negative news cluster: NVDA",
                                      kind="alert", dedupe_hours=12)
        self.assertFalse(first["deduped"])
        self.assertTrue(again["deduped"])
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(tracking.inbox("trader")), 1)
        # A different subject is not suppressed.
        other = tracking.send_message("analyst", "trader", "Negative news cluster: TSLA",
                                      kind="alert", dedupe_hours=12)
        self.assertFalse(other["deduped"])

    def test_subject_required(self):
        with self.assertRaises(ValueError):
            tracking.send_message("a", "b", "  ")

    def test_messages_sent_filter(self):
        tracking.send_message("analyst", "trader", "x", ts="2026-08-01T10:00:00Z")
        tracking.send_message("analyst", "trader", "y", ts="2026-08-05T10:00:00Z")
        tracking.send_message("trader", "analyst", "z", ts="2026-08-05T11:00:00Z")
        sent = tracking.messages_sent("analyst", since="2026-08-05T00:00:00Z")
        self.assertEqual([m["subject"] for m in sent], ["y"])


class AgentStatusTestCase(unittest.TestCase):
    """agent_status() is what the Neighborhood draws each house from."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = tracking.DB_PATH
        tracking.DB_PATH = Path(self._tmp.name) / "s.db"
        tracking.init()

    def tearDown(self):
        tracking.DB_PATH = self._old_db
        self._tmp.cleanup()

    def test_reports_last_scan_and_mail_count(self):
        tracking.log_event("risk_scan", source="risk_manager", payload={"risk_level": "low"})
        tracking.log_event("risk_scan", source="risk_manager", payload={"risk_level": "high"})
        tracking.send_message("risk_manager", "trader", "Portfolio risk: Leverage")
        tracking.send_message("risk_manager", "trader", "Portfolio risk: Concentration")

        st = tracking.agent_status("risk_manager", "risk_scan")
        # The newest scan wins, so the house shows current state, not history.
        self.assertEqual(st["last_scan"]["payload"], {"risk_level": "high"})
        self.assertEqual(st["sent_today"], 2)

    def test_silent_agent_reports_no_scan(self):
        st = tracking.agent_status("pattern_engine", "pattern_scan")
        self.assertIsNone(st["last_scan"])
        self.assertEqual(st["sent_today"], 0)

    def test_scan_kinds_do_not_bleed_between_agents(self):
        tracking.log_event("news_scan", source="analyst", payload={"n_headlines": 40})
        tracking.send_message("analyst", "trader", "Market news digest")

        pm = tracking.agent_status("position_manager", "position_scan")
        self.assertIsNone(pm["last_scan"])
        self.assertEqual(pm["sent_today"], 0)

    def test_every_resident_has_a_distinct_scan_kind(self):
        kinds = list(tracking.TOWN_RESIDENTS.values())
        self.assertEqual(len(kinds), len(set(kinds)))
        for agent, kind in tracking.TOWN_RESIDENTS.items():
            tracking.log_event(kind, source=agent, payload={"agent": agent})
        for agent, kind in tracking.TOWN_RESIDENTS.items():
            st = tracking.agent_status(agent, kind)
            self.assertEqual(st["last_scan"]["payload"]["agent"], agent)


if __name__ == "__main__":
    unittest.main()
