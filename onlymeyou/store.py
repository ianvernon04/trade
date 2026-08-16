"""Only Me & You — the data heart of the couples' prototype.

Everything the app *decides* lives here as plain functions over SQLite:
when a sealed note may be opened, what a locked note is allowed to reveal,
whose reaction lands on a photo, which hearts are news to whom. The FastAPI
server is a thin HTTP skin over these calls. Keeping the rules here — with
`now` injectable everywhere time matters — means the whole promise of the
app ("you can't peek before the time I chose") is enforced and unit-tested
without a web stack or a network, matching this repo's test conventions.

Two people only, called 'a' and 'b'. Display names live in the single
`couple` row; devices remember which letter they are.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"          # tests monkeypatch these three
MEDIA_DIR = DATA_DIR / "media"
DB_PATH = DATA_DIR / "onlymeyou.db"

USERS = ("a", "b")
KINDS = ("voice", "video", "text")

MAX_PHOTO_BYTES = 30 * 1024 * 1024   # phone photos are ~2-8 MB
MAX_NOTE_BYTES = 80 * 1024 * 1024    # "little videos" — a minute or two
MAX_TEXT = 4000

# Media arrives with whatever mime the phone recorded; keep a boring,
# explicit map so saved filenames stay predictable and servable.
_EXT_FOR = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
    "image/gif": "gif", "image/heic": "heic", "image/heif": "heif",
    "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/aac": "aac",
    "audio/mpeg": "mp3", "audio/ogg": "ogg", "audio/wav": "wav",
    "audio/webm": "webm",
    "video/mp4": "mp4", "video/quicktime": "mov", "video/webm": "webm",
    "video/3gpp": "3gp",
}

_write_lock = threading.Lock()


class LockedError(Exception):
    """The note exists but its unlock time has not arrived for this user."""


class NotAllowedError(Exception):
    """The requester is not a party to this item."""


# --------------------------------------------------------------------------
# time helpers

def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    """One canonical on-disk format so ISO strings compare lexicographically."""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str) -> datetime:
    """Accept JS toISOString ('...Z'), offsets, or naive-as-UTC."""
    s = (s or "").strip()
    if not s:
        raise ValueError("empty timestamp")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def other(user: str) -> str:
    return "b" if user == "a" else "a"


def clean_content_type(ct: str | None) -> str:
    return (ct or "").split(";")[0].strip().lower()


def ext_for(content_type: str | None, filename_hint: str | None = None) -> str:
    ct = clean_content_type(content_type)
    if ct in _EXT_FOR:
        return _EXT_FOR[ct]
    if filename_hint and "." in filename_hint:
        tail = filename_hint.rsplit(".", 1)[-1].lower()
        if tail.isalnum() and len(tail) <= 5:
            return tail
    return "bin"


# --------------------------------------------------------------------------
# connection / schema

def _conn() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    return db


def init() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    with _conn() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS couple(
                id INTEGER PRIMARY KEY CHECK (id = 1),
                name_a TEXT NOT NULL DEFAULT 'Me',
                name_b TEXT NOT NULL DEFAULT 'You',
                anniversary TEXT,
                pin_hash TEXT,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photos(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                uploaded_by TEXT NOT NULL,
                filename TEXT NOT NULL,
                content_type TEXT NOT NULL,
                caption TEXT NOT NULL DEFAULT '',
                taken_at TEXT,
                created_at TEXT NOT NULL,
                reactions TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS notes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user TEXT NOT NULL,
                to_user TEXT NOT NULL,
                kind TEXT NOT NULL,
                media_file TEXT,
                content_type TEXT,
                text TEXT NOT NULL DEFAULT '',
                unlock_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                opened_at TEXT
            );
            CREATE TABLE IF NOT EXISTS hearts(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_user TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )


# --------------------------------------------------------------------------
# couple / pin

def _hash_pin(pin: str) -> str:
    return hashlib.sha256(("omy:" + pin).encode("utf-8")).hexdigest()


def get_couple() -> dict | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM couple WHERE id = 1").fetchone()
    if row is None:
        return None
    return {
        "name_a": row["name_a"],
        "name_b": row["name_b"],
        "anniversary": row["anniversary"],
        "has_pin": bool(row["pin_hash"]),
    }


def setup_couple(name_a: str, name_b: str, anniversary: str | None = None,
                 pin: str | None = None, now: datetime | None = None) -> dict:
    if get_couple() is not None:
        raise ValueError("already set up")
    name_a = (name_a or "Me").strip()[:40] or "Me"
    name_b = (name_b or "You").strip()[:40] or "You"
    with _write_lock, _conn() as db:
        db.execute(
            "INSERT INTO couple(id, name_a, name_b, anniversary, pin_hash, created_at)"
            " VALUES (1, ?, ?, ?, ?, ?)",
            (name_a, name_b, (anniversary or "").strip() or None,
             _hash_pin(pin) if pin else None, iso(now or utcnow())),
        )
    return get_couple() or {}


def update_couple(fields: dict) -> dict:
    """Partial update. `pin` of '' clears the PIN; absent leaves it alone."""
    if get_couple() is None:
        raise ValueError("not set up")
    sets, vals = [], []
    for col in ("name_a", "name_b"):
        if col in fields and (fields[col] or "").strip():
            sets.append(f"{col} = ?")
            vals.append(fields[col].strip()[:40])
    if "anniversary" in fields:
        sets.append("anniversary = ?")
        vals.append((fields["anniversary"] or "").strip() or None)
    if "pin" in fields:
        pin = (fields["pin"] or "").strip()
        sets.append("pin_hash = ?")
        vals.append(_hash_pin(pin) if pin else None)
    if sets:
        with _write_lock, _conn() as db:
            db.execute(f"UPDATE couple SET {', '.join(sets)} WHERE id = 1", vals)
    return get_couple() or {}


def check_pin(pin: str | None) -> bool:
    """True when the given PIN unlocks the couple (or no PIN is set)."""
    couple = get_couple()
    if couple is None or not couple["has_pin"]:
        return True
    if not pin:
        return False
    with _conn() as db:
        row = db.execute("SELECT pin_hash FROM couple WHERE id = 1").fetchone()
    return bool(row) and secrets.compare_digest(row["pin_hash"], _hash_pin(pin))


# --------------------------------------------------------------------------
# media files

def save_media(data: bytes, content_type: str | None, prefix: str,
               filename_hint: str | None = None) -> tuple[str, str]:
    """Write bytes under MEDIA_DIR with a random, servable name."""
    ct = clean_content_type(content_type) or "application/octet-stream"
    name = f"{prefix}_{secrets.token_hex(8)}.{ext_for(content_type, filename_hint)}"
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / name).write_bytes(data)
    return name, ct


def media_path(filename: str) -> Path | None:
    """Resolve a stored filename inside MEDIA_DIR, refusing anything fancy."""
    if not filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    path = MEDIA_DIR / filename
    return path if path.is_file() else None


# --------------------------------------------------------------------------
# photos

def _photo_dict(row: sqlite3.Row) -> dict:
    try:
        reactions = json.loads(row["reactions"] or "{}")
    except ValueError:
        reactions = {}
    return {
        "id": row["id"],
        "by": row["uploaded_by"],
        "filename": row["filename"],
        "content_type": row["content_type"],
        "caption": row["caption"],
        "taken_at": row["taken_at"],
        "created_at": row["created_at"],
        "at": row["taken_at"] or row["created_at"],
        "reactions": reactions,
    }


def add_photo(user: str, data: bytes, content_type: str | None,
              caption: str = "", taken_at: str | None = None,
              filename_hint: str | None = None,
              now: datetime | None = None) -> dict:
    if user not in USERS:
        raise ValueError("unknown user")
    if not data:
        raise ValueError("empty photo")
    taken_iso = iso(parse_iso(taken_at)) if taken_at else None
    fname, ct = save_media(data, content_type, "photo", filename_hint)
    with _write_lock, _conn() as db:
        cur = db.execute(
            "INSERT INTO photos(uploaded_by, filename, content_type, caption,"
            " taken_at, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user, fname, ct, (caption or "").strip()[:MAX_TEXT], taken_iso,
             iso(now or utcnow())),
        )
        row = db.execute("SELECT * FROM photos WHERE id = ?",
                         (cur.lastrowid,)).fetchone()
    return _photo_dict(row)


def list_photos() -> list[dict]:
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM photos ORDER BY COALESCE(taken_at, created_at) DESC,"
            " id DESC"
        ).fetchall()
    return [_photo_dict(r) for r in rows]


def get_photo(photo_id: int) -> dict | None:
    with _conn() as db:
        row = db.execute("SELECT * FROM photos WHERE id = ?",
                         (photo_id,)).fetchone()
    return _photo_dict(row) if row else None


def react_photo(photo_id: int, user: str, emoji: str) -> dict | None:
    """Each partner holds one reaction per photo; empty emoji clears it."""
    if user not in USERS:
        raise ValueError("unknown user")
    photo = get_photo(photo_id)
    if photo is None:
        return None
    reactions = photo["reactions"]
    emoji = (emoji or "").strip()[:8]
    if emoji:
        reactions[user] = emoji
    else:
        reactions.pop(user, None)
    with _write_lock, _conn() as db:
        db.execute("UPDATE photos SET reactions = ? WHERE id = ?",
                   (json.dumps(reactions, ensure_ascii=False), photo_id))
    return get_photo(photo_id)


def delete_photo(photo_id: int) -> bool:
    photo = get_photo(photo_id)
    if photo is None:
        return False
    with _write_lock, _conn() as db:
        db.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
    path = media_path(photo["filename"])
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass  # the row is gone; a stray file is harmless
    return True


# --------------------------------------------------------------------------
# notes (time-locked voice / video / text)

def add_note(from_user: str, kind: str, unlock_at: str, text: str = "",
             data: bytes | None = None, content_type: str | None = None,
             filename_hint: str | None = None,
             now: datetime | None = None) -> dict:
    if from_user not in USERS:
        raise ValueError("unknown user")
    if kind not in KINDS:
        raise ValueError("unknown kind")
    if kind == "text" and not (text or "").strip():
        raise ValueError("empty note")
    if kind in ("voice", "video") and not data:
        raise ValueError("missing media")
    unlock_iso = iso(parse_iso(unlock_at))
    fname = ct = None
    if data:
        fname, ct = save_media(data, content_type, kind, filename_hint)
    with _write_lock, _conn() as db:
        cur = db.execute(
            "INSERT INTO notes(from_user, to_user, kind, media_file,"
            " content_type, text, unlock_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (from_user, other(from_user), kind, fname, ct,
             (text or "").strip()[:MAX_TEXT], unlock_iso,
             iso(now or utcnow())),
        )
        note_id = cur.lastrowid
    return _note_full(_get_note(note_id))


def _get_note(note_id: int) -> sqlite3.Row | None:
    with _conn() as db:
        return db.execute("SELECT * FROM notes WHERE id = ?",
                          (note_id,)).fetchone()


def _is_unlocked(row: sqlite3.Row, now: datetime | None = None) -> bool:
    return iso(now or utcnow()) >= row["unlock_at"]


def _note_full(row: sqlite3.Row | None) -> dict:
    assert row is not None
    return {
        "id": row["id"],
        "from": row["from_user"],
        "to": row["to_user"],
        "kind": row["kind"],
        "text": row["text"],
        "has_media": bool(row["media_file"]),
        "content_type": row["content_type"],
        "unlock_at": row["unlock_at"],
        "created_at": row["created_at"],
        "opened_at": row["opened_at"],
    }


def _note_sealed(row: sqlite3.Row) -> dict:
    """What a locked note admits to its recipient: who, what shape, when.

    Deliberately no text and no media reference — the content *is* the
    surprise, so the API never ships it to the person it's sealed for.
    """
    return {
        "id": row["id"],
        "from": row["from_user"],
        "to": row["to_user"],
        "kind": row["kind"],
        "has_media": bool(row["media_file"]),
        "unlock_at": row["unlock_at"],
        "created_at": row["created_at"],
        "locked": True,
    }


def list_notes(user: str, now: datetime | None = None) -> dict:
    if user not in USERS:
        raise ValueError("unknown user")
    with _conn() as db:
        rows = db.execute(
            "SELECT * FROM notes ORDER BY created_at DESC, id DESC"
        ).fetchall()
    inbox, sent = [], []
    for row in rows:
        unlocked = _is_unlocked(row, now)
        if row["to_user"] == user:
            if unlocked:
                d = _note_full(row)
                d["locked"] = False
                inbox.append(d)
            else:
                inbox.append(_note_sealed(row))
        elif row["from_user"] == user:
            d = _note_full(row)
            d["locked"] = not unlocked
            sent.append(d)
    return {"inbox": inbox, "sent": sent}


def open_note(note_id: int, user: str, now: datetime | None = None) -> dict:
    """Recipient opens an unlocked note; first open is stamped."""
    row = _get_note(note_id)
    if row is None:
        raise KeyError("no such note")
    if row["to_user"] != user:
        raise NotAllowedError("only the recipient can open a note")
    if not _is_unlocked(row, now):
        raise LockedError(row["unlock_at"])
    if row["opened_at"] is None:
        with _write_lock, _conn() as db:
            db.execute("UPDATE notes SET opened_at = ? WHERE id = ?",
                       (iso(now or utcnow()), note_id))
        row = _get_note(note_id)
    d = _note_full(row)
    d["locked"] = False
    return d


def note_media(note_id: int, user: str,
               now: datetime | None = None) -> tuple[Path, str]:
    """Path+mime for a note's media. Senders may always replay their own;
    recipients only once the clock says so."""
    row = _get_note(note_id)
    if row is None or not row["media_file"]:
        raise KeyError("no media")
    if user == row["from_user"]:
        pass
    elif user == row["to_user"]:
        if not _is_unlocked(row, now):
            raise LockedError(row["unlock_at"])
    else:
        raise NotAllowedError("not yours")
    path = media_path(row["media_file"])
    if path is None:
        raise KeyError("media file missing")
    return path, row["content_type"] or "application/octet-stream"


# --------------------------------------------------------------------------
# hearts (tiny pings, purely for delight)

def add_heart(user: str, now: datetime | None = None) -> dict:
    if user not in USERS:
        raise ValueError("unknown user")
    at = iso(now or utcnow())
    with _write_lock, _conn() as db:
        db.execute("INSERT INTO hearts(from_user, created_at) VALUES (?, ?)",
                   (user, at))
    return {"from": user, "created_at": at}


def hearts_for(user: str, since: str | None = None,
               limit: int = 50) -> list[str]:
    """Hearts sent *to* `user` (i.e. by the other one) after `since`."""
    if user not in USERS:
        raise ValueError("unknown user")
    q = "SELECT created_at FROM hearts WHERE from_user = ?"
    args: list = [other(user)]
    if since:
        try:
            q += " AND created_at > ?"
            args.append(iso(parse_iso(since)))
        except ValueError:
            pass  # bad cursor → just return the recent ones
    q += " ORDER BY created_at DESC LIMIT ?"
    args.append(max(1, min(limit, 200)))
    with _conn() as db:
        rows = db.execute(q, args).fetchall()
    return [r["created_at"] for r in rows]


# --------------------------------------------------------------------------
# dashboard counts

def counts(user: str, now: datetime | None = None) -> dict:
    now_iso = iso(now or utcnow())
    with _conn() as db:
        photos = db.execute("SELECT COUNT(*) c FROM photos").fetchone()["c"]
        ready = db.execute(
            "SELECT COUNT(*) c FROM notes WHERE to_user = ? AND unlock_at <= ?"
            " AND opened_at IS NULL", (user, now_iso)).fetchone()["c"]
        locked = db.execute(
            "SELECT COUNT(*) c FROM notes WHERE to_user = ? AND unlock_at > ?",
            (user, now_iso)).fetchone()["c"]
        next_unlock = db.execute(
            "SELECT MIN(unlock_at) m FROM notes WHERE to_user = ?"
            " AND unlock_at > ?", (user, now_iso)).fetchone()["m"]
        sent_sealed = db.execute(
            "SELECT COUNT(*) c FROM notes WHERE from_user = ?"
            " AND opened_at IS NULL", (user,)).fetchone()["c"]
        latest = db.execute(
            "SELECT uploaded_by, COALESCE(taken_at, created_at) at,"
            " created_at FROM photos ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
    return {
        "photos": photos,
        "inbox_ready": ready,
        "inbox_locked": locked,
        "next_unlock": next_unlock,
        "sent_sealed": sent_sealed,
        "latest_photo": (
            {"by": latest["uploaded_by"], "at": latest["at"],
             "created_at": latest["created_at"]} if latest else None),
    }
