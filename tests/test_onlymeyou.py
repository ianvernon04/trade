"""Unit tests for the Only Me & You store (no network, injected time).

The one promise that matters most here is the time-lock: a sealed note must
reveal nothing — not its text, not its media — to its recipient until the
unlock moment, while its sender can always replay it. These tests pin that
behaviour down, because the UI can only be as trustworthy as this module.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from onlymeyou import store

T0 = datetime(2026, 8, 16, 12, 0, tzinfo=timezone.utc)


class OnlyMeYouTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._old = (store.DATA_DIR, store.MEDIA_DIR, store.DB_PATH)
        store.DATA_DIR = root
        store.MEDIA_DIR = root / "media"
        store.DB_PATH = root / "test.db"
        store.init()
        store._pin_fails.clear()

    def tearDown(self):
        store.DATA_DIR, store.MEDIA_DIR, store.DB_PATH = self._old
        self._tmp.cleanup()

    # ---------------- couple / pin ----------------

    def test_setup_and_pin(self):
        self.assertIsNone(store.get_couple())
        self.assertTrue(store.check_pin(None))  # nothing to protect yet
        couple = store.setup_couple("Ian", "Mia", anniversary="2024-02-14",
                                    pin="2214", now=T0)
        self.assertEqual(couple["name_a"], "Ian")
        self.assertTrue(couple["has_pin"])
        self.assertTrue(store.check_pin("2214"))
        self.assertFalse(store.check_pin("0000"))
        self.assertFalse(store.check_pin(None))
        with self.assertRaises(ValueError):
            store.setup_couple("X", "Y")  # only one couple per world

    def test_pin_update_and_removal(self):
        store.setup_couple("A", "B", pin="1111", now=T0)
        store.update_couple({"pin": "9999"})
        self.assertTrue(store.check_pin("9999"))
        self.assertFalse(store.check_pin("1111"))
        store.update_couple({"pin": ""})
        self.assertTrue(store.check_pin(None))  # cleared

    def test_partial_update_keeps_missing_fields(self):
        store.setup_couple("A", "B", anniversary="2024-01-01", now=T0)
        store.update_couple({"name_b": "Bee"})
        couple = store.get_couple()
        self.assertEqual(couple["name_a"], "A")
        self.assertEqual(couple["name_b"], "Bee")
        self.assertEqual(couple["anniversary"], "2024-01-01")

    def test_data_dir_env_override(self):
        """OMY_DATA_DIR points the whole world at a persistent disk."""
        import importlib
        import os
        target = Path(self._tmp.name) / "envdata"
        os.environ["OMY_DATA_DIR"] = str(target)
        try:
            importlib.reload(store)
            self.assertEqual(store.DATA_DIR, target)
            self.assertEqual(store.MEDIA_DIR, target / "media")
            self.assertEqual(store.DB_PATH, target / "onlymeyou.db")
        finally:
            del os.environ["OMY_DATA_DIR"]
            importlib.reload(store)  # back to defaults; tearDown re-patches

    # ---------------- pin throttle ----------------

    def test_pin_throttle_locks_an_address_out(self):
        self.assertTrue(store.pin_throttle_ok("1.2.3.4", now=T0))
        for _ in range(store.PIN_MAX_FAILS):
            store.pin_throttle_note("1.2.3.4", ok=False, now=T0)
        self.assertFalse(store.pin_throttle_ok("1.2.3.4", now=T0))
        self.assertTrue(store.pin_throttle_ok("5.6.7.8", now=T0))  # others fine
        later = T0 + timedelta(seconds=store.PIN_WINDOW_SECONDS + 1)
        self.assertTrue(store.pin_throttle_ok("1.2.3.4", now=later))

    def test_pin_throttle_success_clears_the_address(self):
        for _ in range(store.PIN_MAX_FAILS - 1):
            store.pin_throttle_note("1.2.3.4", ok=False, now=T0)
        store.pin_throttle_note("1.2.3.4", ok=True, now=T0)
        self.assertTrue(store.pin_throttle_ok("1.2.3.4", now=T0))

    def test_pin_throttle_global_backstop(self):
        # a bot rotating spoofed addresses still hits the shared ceiling
        for i in range(store.PIN_GLOBAL_MAX_FAILS):
            store.pin_throttle_note(f"10.0.0.{i}", ok=False, now=T0)
        self.assertFalse(store.pin_throttle_ok("99.99.99.99", now=T0))

    # ---------------- time parsing ----------------

    def test_parse_iso_variants(self):
        js = store.parse_iso("2026-08-16T20:00:00.000Z")  # JS toISOString
        py = store.parse_iso("2026-08-16T20:00:00+00:00")
        naive = store.parse_iso("2026-08-16T20:00:00")
        self.assertEqual(js, py)
        self.assertEqual(naive, py)
        with self.assertRaises(ValueError):
            store.parse_iso("not a time")

    # ---------------- photos ----------------

    def test_photo_roundtrip_order_and_reactions(self):
        store.setup_couple("A", "B", now=T0)
        old = store.add_photo("a", b"OLD", "image/jpeg", caption="first date",
                              taken_at="2026-07-01T10:00:00Z", now=T0)
        new = store.add_photo("b", b"NEW", "image/png", now=T0)
        photos = store.list_photos()
        self.assertEqual([p["id"] for p in photos], [new["id"], old["id"]])
        self.assertEqual(photos[1]["caption"], "first date")
        self.assertTrue(photos[1]["at"].startswith("2026-07-01"))
        self.assertTrue((store.MEDIA_DIR / old["filename"]).is_file())
        self.assertTrue(old["filename"].endswith(".jpg"))

        store.react_photo(old["id"], "b", "😍")
        store.react_photo(old["id"], "b", "❤️")  # overwrite own reaction
        store.react_photo(old["id"], "a", "🥰")
        got = store.get_photo(old["id"])
        self.assertEqual(got["reactions"], {"b": "❤️", "a": "🥰"})
        store.react_photo(old["id"], "b", "")  # clear
        self.assertEqual(store.get_photo(old["id"])["reactions"], {"a": "🥰"})

    def test_photo_delete_removes_file(self):
        store.setup_couple("A", "B", now=T0)
        p = store.add_photo("a", b"XX", "image/jpeg", now=T0)
        path = store.MEDIA_DIR / p["filename"]
        self.assertTrue(path.is_file())
        self.assertTrue(store.delete_photo(p["id"]))
        self.assertFalse(path.exists())
        self.assertIsNone(store.get_photo(p["id"]))
        self.assertFalse(store.delete_photo(p["id"]))

    # ---------------- the time-lock ----------------

    def test_sealed_note_reveals_nothing_to_recipient(self):
        store.setup_couple("A", "B", now=T0)
        unlock = store.iso(T0 + timedelta(hours=6))
        note = store.add_note("a", "text", unlock, text="marry me again", now=T0)
        self.assertEqual(note["to"], "b")

        boxes = store.list_notes("b", now=T0 + timedelta(hours=1))
        self.assertEqual(len(boxes["inbox"]), 1)
        sealed = boxes["inbox"][0]
        self.assertTrue(sealed["locked"])
        self.assertNotIn("text", sealed)       # the surprise stays a surprise
        self.assertNotIn("opened_at", sealed)
        # sender still sees their own words
        sent = store.list_notes("a", now=T0 + timedelta(hours=1))["sent"][0]
        self.assertEqual(sent["text"], "marry me again")
        self.assertTrue(sent["locked"])

    def test_open_respects_the_clock(self):
        store.setup_couple("A", "B", now=T0)
        unlock = store.iso(T0 + timedelta(hours=6))
        note = store.add_note("a", "text", unlock, text="hi", now=T0)
        with self.assertRaises(store.LockedError):
            store.open_note(note["id"], "b", now=T0 + timedelta(hours=5))
        with self.assertRaises(store.NotAllowedError):
            store.open_note(note["id"], "a", now=T0 + timedelta(hours=7))
        opened = store.open_note(note["id"], "b", now=T0 + timedelta(hours=6))
        self.assertEqual(opened["text"], "hi")
        first_stamp = opened["opened_at"]
        again = store.open_note(note["id"], "b", now=T0 + timedelta(hours=8))
        self.assertEqual(again["opened_at"], first_stamp)  # first open sticks

    def test_media_lock_sender_can_replay_recipient_waits(self):
        store.setup_couple("A", "B", now=T0)
        unlock = store.iso(T0 + timedelta(days=1))
        note = store.add_note("a", "voice", unlock, data=b"AUDIO",
                              content_type="audio/mp4", now=T0)
        path, ct = store.note_media(note["id"], "a", now=T0)  # sender replays
        self.assertEqual(ct, "audio/mp4")
        self.assertEqual(path.read_bytes(), b"AUDIO")
        self.assertTrue(path.name.endswith(".m4a"))
        with self.assertRaises(store.LockedError):
            store.note_media(note["id"], "b", now=T0 + timedelta(hours=23))
        path2, _ = store.note_media(note["id"], "b", now=T0 + timedelta(days=1))
        self.assertEqual(path2, path)

    def test_note_validation(self):
        store.setup_couple("A", "B", now=T0)
        with self.assertRaises(ValueError):
            store.add_note("a", "text", store.iso(T0), text="   ", now=T0)
        with self.assertRaises(ValueError):
            store.add_note("a", "voice", store.iso(T0), data=None, now=T0)
        with self.assertRaises(ValueError):
            store.add_note("a", "smoke_signal", store.iso(T0), text="x", now=T0)
        with self.assertRaises(ValueError):
            store.add_note("c", "text", store.iso(T0), text="x", now=T0)

    # ---------------- hearts ----------------

    def test_hearts_flow_to_the_other_person(self):
        store.setup_couple("A", "B", now=T0)
        store.add_heart("a", now=T0)
        store.add_heart("a", now=T0 + timedelta(minutes=5))
        self.assertEqual(len(store.hearts_for("b")), 2)
        self.assertEqual(store.hearts_for("a"), [])  # own hearts aren't news
        since = store.iso(T0 + timedelta(minutes=1))
        self.assertEqual(len(store.hearts_for("b", since=since)), 1)

    # ---------------- counts ----------------

    def test_counts_track_locks_over_time(self):
        store.setup_couple("A", "B", now=T0)
        store.add_photo("a", b"P", "image/jpeg", now=T0)
        store.add_note("a", "text", store.iso(T0 + timedelta(hours=2)),
                       text="soon", now=T0)
        store.add_note("a", "text", store.iso(T0 + timedelta(days=3)),
                       text="later", now=T0)
        c1 = store.counts("b", now=T0 + timedelta(hours=1))
        self.assertEqual((c1["inbox_ready"], c1["inbox_locked"]), (0, 2))
        self.assertEqual(c1["next_unlock"], store.iso(T0 + timedelta(hours=2)))
        c2 = store.counts("b", now=T0 + timedelta(hours=2))
        self.assertEqual((c2["inbox_ready"], c2["inbox_locked"]), (1, 1))
        self.assertEqual(c2["photos"], 1)
        self.assertEqual(store.counts("a", now=T0)["sent_sealed"], 2)
        self.assertEqual(c2["latest_photo"]["by"], "a")

    # ---------------- filenames / media safety ----------------

    def test_media_path_refuses_traversal(self):
        store.setup_couple("A", "B", now=T0)
        self.assertIsNone(store.media_path("../secrets.txt"))
        self.assertIsNone(store.media_path("a/b.png"))
        self.assertIsNone(store.media_path(""))

    def test_ext_mapping(self):
        self.assertEqual(store.ext_for("audio/webm;codecs=opus"), "webm")
        self.assertEqual(store.ext_for("video/quicktime"), "mov")
        self.assertEqual(store.ext_for("application/x-mystery", "clip.MP4"), "mp4")
        self.assertEqual(store.ext_for("application/x-mystery"), "bin")


if __name__ == "__main__":
    unittest.main()
