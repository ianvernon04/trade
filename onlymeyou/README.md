# Only Me & You 💞

A tiny private world for two — a prototype couples' app you run yourselves.
No accounts, no cloud, no feed of strangers: one little server, two phones,
and everything stays on your own machine.

| Home | Love notes | Sealing a note |
| --- | --- | --- |
| ![home](docs/home.png) | ![notes](docs/notes.png) | ![compose](docs/compose.png) |

**What's inside**

- 📸 **Our moments** — a shared photo timeline. Every picture keeps its date
  and time (you can backdate a memory), grouped by month, with captions and
  emoji reactions. Double-tap a photo to leave a ❤️.
- 💌 **Love notes** — send each other little **videos**, **voice messages**,
  and **letters**, and *choose when the other person can open them*: right
  away, tonight at 8, tomorrow morning, or any exact date and time. Until
  then it sits sealed as a 🎁 with a live countdown — the server won't hand
  over the content early, so peeking genuinely isn't possible.
- 💗 **Hearts** — one tap sends a little heart that pops up on the other
  phone within seconds.
- ⏱️ **Together-for counter** — set your special date once and watch the
  days/hours/minutes/seconds tick up on the home screen, next to a live clock.
- 🔒 An optional **couple PIN** keeps everyone else out.

## Run it

From the repo root (Python 3.10+):

```bash
pip install -r requirements.txt
python3 -m onlymeyou            # → http://<your-computer>:9000
```

The banner prints two URLs. Open the **phones** one (same Wi-Fi) on both
phones, walk through the 30-second setup (names, your date, a PIN), and each
phone picks who it belongs to. That's it.

### Put it on your phones like a real app 📲

- **iPhone** — open the URL in Safari → **Share** → **Add to Home Screen**.
- **Android** — open it in Chrome → **⋮** → **Add to Home screen** / **Install**.

You get a 💞 icon and a full-screen app, no browser bars.

### When you're not on the same Wi-Fi (or want the mic to work)

Browsers only allow **in-app microphone recording** (and full PWA install
with offline shell) on HTTPS. Two ways to get there:

**Free, while your computer is on** — [Tailscale](https://tailscale.com/download)
on the computer and both phones (same account on all three), then
`tailscale serve --bg 9000` gives you a stable, private
`https://…ts.net` URL only your devices can reach. (A
`cloudflared tunnel --url http://localhost:9000` quick tunnel also works,
but its URL changes on every restart — fine for a test, wrong for a
home-screen icon.)

**Always on, no computer needed (~$8/mo)** — host it on
[Render](https://render.com) with the blueprint in this repo:

1. Merge/push this repo to GitHub, then in Render: **New + → Blueprint** →
   pick the repo. It reads `render.yaml` and offers its services — you want
   **`only-me-and-you`** (deploy the trading assistant too or delete it,
   they're independent).
2. Approve the plan: Starter instance ($7/mo) plus a 5 GB persistent disk
   ($1.25/mo) mounted at `/var/data`, where all photos/notes/DB live via
   `OMY_DATA_DIR`. **Don't downgrade to the free plan** — free instances
   have no disk, and every restart would erase your memories.
3. When the deploy finishes you get a permanent
   `https://only-me-and-you-….onrender.com` URL. Open it on both phones,
   run setup, **choose a longer PIN (6–8 digits)** since the URL is on the
   public internet, and Add to Home Screen. Mic recording works (it's HTTPS).
4. Pushes to `main` auto-deploy; your data survives deploys because it
   lives on the disk. Wrong-PIN attempts are rate-limited server-side
   (8/min per address, 30/min overall) so a short PIN can't be
   brute-forced quickly — but longer is still better.

Over plain-HTTP Wi-Fi everything else still works — video notes use the
phone's native camera, and voice notes fall back to picking an audio file.

## Where your stuff lives

Everything — database, photos, voice and video files — is in
`onlymeyou/data/` (gitignored; it never leaves your machine unless you
tunnel). Back it up by copying that folder. Set `OMY_DATA_DIR` to move it —
that's how the Render deploy keeps everything on its persistent disk at
`/var/data`.

## Honest prototype notes

- One couple per server; the PIN is a friendly lock, not bank security.
- The **time-lock is enforced server-side** against the server's clock: the
  sealed content and its media file are simply never sent to the recipient
  before the unlock moment. (The sender can always replay their own notes.)
- iPhone videos record in HEVC by default, which some Android phones won't
  play. If that bites you: iPhone Settings → Camera → Formats → **Most
  Compatible**.
- Notifications are in-app (it checks every few seconds while open) — real
  push notifications would be the next step for v2.

## Dev

```bash
python3 -m unittest discover -s tests       # includes tests/test_onlymeyou.py
python3 -m onlymeyou --port 9000            # run
python3 -m onlymeyou.tools.make_icons       # regenerate the heart icons
```

Module map: `store.py` (SQLite + all the rules, `now` injectable),
`server.py` (FastAPI skin: raw-body uploads, PIN gate, Range-aware media),
`static/` (the PWA — vanilla JS, no build step).
