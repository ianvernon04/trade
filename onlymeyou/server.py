"""Only Me & You — the HTTP skin over `onlymeyou.store`.

Design constraints that shaped this file, so they survive refactors:

- Uploads are **raw request bodies** (metadata in query params), not
  multipart forms — the repo deliberately has no `python-multipart`
  dependency, and two-person media uploads don't need one.
- Media is served through id-routes (`/api/.../media`) rather than a
  static mount, because access is a *rule*: a sealed note's file must be
  unreachable for its recipient until the unlock time, and everything is
  behind the couple's PIN when one is set.
- The media route honours HTTP Range requests by hand. iOS Safari probes
  `Range: bytes=0-1` before it will play audio/video; Starlette's
  FileResponse doesn't answer that, and a love note that won't play on an
  iPhone is a broken promise.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import store

STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Only Me & You", docs_url=None, redoc_url=None)
store.init()


# --------------------------------------------------------------------------
# small helpers

def _err(status: int, message: str) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


def _pin_from(request: Request) -> str | None:
    return request.headers.get("x-pin") or request.cookies.get("omy_pin")


def _client_ip(request: Request) -> str:
    # Behind a hosting proxy (Render et al.) the real caller is the first
    # X-Forwarded-For hop; locally it's the socket peer.
    fwd = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if fwd:
        return fwd
    return request.client.host if request.client else "?"


def _authed(request: Request) -> bool:
    return store.check_pin(_pin_from(request))


def _user_from(request: Request) -> str | None:
    user = request.headers.get("x-user") or request.cookies.get("omy_user")
    return user if user in store.USERS else None


async def _read_body(request: Request, max_bytes: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ValueError(f"too large (limit {max_bytes // (1024 * 1024)} MB)")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            raise ValueError(f"too large (limit {max_bytes // (1024 * 1024)} MB)")
        chunks.append(chunk)
    return b"".join(chunks)


_RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


def _media_response(path: Path, content_type: str, request: Request) -> Response:
    """FileResponse, plus honest 206 handling for Safari's Range probes."""
    size = path.stat().st_size
    match = _RANGE_RE.match(request.headers.get("range", "").strip())
    if not match or size == 0:
        return FileResponse(path, media_type=content_type,
                            headers={"Accept-Ranges": "bytes"})
    start_s, end_s = match.groups()
    if start_s:
        start = int(start_s)
        end = min(int(end_s), size - 1) if end_s else size - 1
    elif end_s:  # suffix form: last N bytes
        start = max(0, size - int(end_s))
        end = size - 1
    else:
        return FileResponse(path, media_type=content_type,
                            headers={"Accept-Ranges": "bytes"})
    if start >= size or start > end:
        return Response(status_code=416,
                        headers={"Content-Range": f"bytes */{size}"})

    def stream(first: int = start, last: int = end):
        with path.open("rb") as fh:
            fh.seek(first)
            remaining = last - first + 1
            while remaining > 0:
                chunk = fh.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers = {
        "Content-Range": f"bytes {start}-{end}/{size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(end - start + 1),
    }
    return StreamingResponse(stream(), status_code=206, headers=headers,
                             media_type=content_type)


# --------------------------------------------------------------------------
# the PIN gate. /api/state and /api/setup stay reachable so a fresh device
# can learn it needs a PIN (or run first-time setup) — neither leaks data.
# Wrong-PIN attempts are throttled per address (and globally) because on an
# always-on public URL this gate is all that guards two people's photos.

_OPEN_PATHS = {"/api/state", "/api/setup"}


@app.middleware("http")
async def pin_gate(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        ip = _client_ip(request)
        if not store.pin_throttle_ok(ip):
            return _err(429, "too many PIN tries — wait a minute and try again 🕐")
        pin = _pin_from(request)
        authed = store.check_pin(pin)
        if pin and store.pin_is_set():
            store.pin_throttle_note(ip, ok=authed)
        if not authed and path not in _OPEN_PATHS:
            return _err(401, "pin_required")
    return await call_next(request)


# --------------------------------------------------------------------------
# state / setup / settings

@app.get("/api/state")
def api_state(request: Request, hearts_since: str | None = None):
    couple = store.get_couple()
    now = store.iso(store.utcnow())
    if couple is None:
        return {"setup": False, "authed": True, "pin_required": False, "now": now}
    authed = _authed(request)
    out: dict = {
        "setup": True,
        "pin_required": couple["has_pin"],
        "authed": authed,
        "now": now,
    }
    if not authed:
        return out
    user = _user_from(request)
    out["names"] = {"a": couple["name_a"], "b": couple["name_b"]}
    out["anniversary"] = couple["anniversary"]
    out["me"] = user
    if user:
        out["counts"] = store.counts(user)
        out["hearts"] = store.hearts_for(user, since=hearts_since)
    return out


@app.post("/api/setup")
async def api_setup(request: Request):
    try:
        body = json.loads(await _read_body(request, 64 * 1024) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return _err(400, "bad json")
    try:
        couple = store.setup_couple(
            name_a=str(body.get("name_a", "")),
            name_b=str(body.get("name_b", "")),
            anniversary=str(body.get("anniversary", "") or "") or None,
            pin=str(body.get("pin", "") or "") or None,
        )
    except ValueError as exc:
        return _err(409, str(exc))
    return {"ok": True, "couple": couple}


@app.post("/api/settings")
async def api_settings(request: Request):
    try:
        body = json.loads(await _read_body(request, 64 * 1024) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return _err(400, "bad json")
    allowed = {k: str(v) for k, v in body.items()
               if k in ("name_a", "name_b", "anniversary", "pin")}
    try:
        couple = store.update_couple(allowed)
    except ValueError as exc:
        return _err(409, str(exc))
    return {"ok": True, "couple": couple}


# --------------------------------------------------------------------------
# photos

def _photo_out(photo: dict) -> dict:
    out = dict(photo)
    out["media_url"] = f"/api/photos/{photo['id']}/media"
    out.pop("filename", None)
    return out


@app.get("/api/photos")
def api_photos():
    return {"photos": [_photo_out(p) for p in store.list_photos()]}


@app.post("/api/photos")
async def api_add_photo(request: Request, caption: str = "",
                        taken_at: str | None = None, fn: str | None = None):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you? (missing X-User)")
    try:
        data = await _read_body(request, store.MAX_PHOTO_BYTES)
    except ValueError as exc:
        return _err(413, str(exc))
    if not data:
        return _err(400, "empty upload")
    try:
        photo = store.add_photo(
            user, data, request.headers.get("content-type"),
            caption=caption, taken_at=taken_at or None, filename_hint=fn)
    except ValueError as exc:
        return _err(400, str(exc))
    return {"ok": True, "photo": _photo_out(photo)}


@app.get("/api/photos/{photo_id}/media")
def api_photo_media(photo_id: int, request: Request):
    photo = store.get_photo(photo_id)
    if photo is None:
        return _err(404, "no such photo")
    path = store.media_path(photo["filename"])
    if path is None:
        return _err(404, "file missing")
    return _media_response(path, photo["content_type"], request)


@app.post("/api/photos/{photo_id}/react")
async def api_react(photo_id: int, request: Request):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you? (missing X-User)")
    try:
        body = json.loads(await _read_body(request, 4096) or b"{}")
    except (ValueError, json.JSONDecodeError):
        return _err(400, "bad json")
    photo = store.react_photo(photo_id, user, str(body.get("emoji", "")))
    if photo is None:
        return _err(404, "no such photo")
    return {"ok": True, "photo": _photo_out(photo)}


@app.delete("/api/photos/{photo_id}")
def api_delete_photo(photo_id: int):
    if not store.delete_photo(photo_id):
        return _err(404, "no such photo")
    return {"ok": True}


# --------------------------------------------------------------------------
# notes

def _note_out(note: dict) -> dict:
    out = dict(note)
    if out.get("has_media") and not out.get("locked", False):
        out["media_url"] = f"/api/notes/{note['id']}/media"
    out.pop("content_type", None)
    return out


@app.get("/api/notes")
def api_notes(request: Request):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you? (missing X-User)")
    boxes = store.list_notes(user)
    return {
        "inbox": [_note_out(n) for n in boxes["inbox"]],
        "sent": [_note_out(n) for n in boxes["sent"]],
    }


@app.post("/api/notes")
async def api_add_note(request: Request, kind: str | None = None,
                       unlock_at: str | None = None, text: str = "",
                       fn: str | None = None):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you? (missing X-User)")
    content_type = store.clean_content_type(request.headers.get("content-type"))
    try:
        if content_type == "application/json":
            body = json.loads(await _read_body(request, 64 * 1024) or b"{}")
            kind = str(body.get("kind", "text"))
            note = store.add_note(
                user, kind, str(body.get("unlock_at", "")),
                text=str(body.get("text", "")))
        else:
            data = await _read_body(request, store.MAX_NOTE_BYTES)
            note = store.add_note(
                user, kind or "", unlock_at or "", text=text,
                data=data, content_type=request.headers.get("content-type"),
                filename_hint=fn)
    except ValueError as exc:
        return _err(400, str(exc))
    return {"ok": True, "note": _note_out(note)}


@app.post("/api/notes/{note_id}/open")
def api_open_note(note_id: int, request: Request):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you? (missing X-User)")
    try:
        note = store.open_note(note_id, user)
    except KeyError:
        return _err(404, "no such note")
    except store.NotAllowedError:
        return _err(403, "only the recipient can open this")
    except store.LockedError as exc:
        return _err(423, f"still sealed until {exc}")
    return {"ok": True, "note": _note_out(note)}


@app.get("/api/notes/{note_id}/media")
def api_note_media(note_id: int, request: Request):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you?")
    try:
        path, content_type = store.note_media(note_id, user)
    except KeyError:
        return _err(404, "no such media")
    except store.NotAllowedError:
        return _err(403, "not yours")
    except store.LockedError as exc:
        return _err(423, f"still sealed until {exc}")
    return _media_response(path, content_type, request)


# --------------------------------------------------------------------------
# hearts

@app.post("/api/hearts")
def api_send_heart(request: Request):
    user = _user_from(request)
    if user is None:
        return _err(400, "who are you? (missing X-User)")
    heart = store.add_heart(user)
    return {"ok": True, "heart": heart}


# --------------------------------------------------------------------------
# the app shell (registered last so /api/* wins)

app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
