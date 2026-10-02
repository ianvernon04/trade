"""Broker statement importer: real Robinhood fills into the journal.

The journal was only ever as honest as the human typing into it, which in
practice meant it was empty. A track record you cannot show a client is not a
track record. This module takes what the broker itself says happened — the
Robinhood activity CSV, or a monthly statement PDF — turns it into fills, and
pairs those fills into the journal's round-trip trades.

Two rules shape everything here:

1. **Nothing is silently dropped.** A fill this module cannot pair (an exit
   whose entry predates the export, an assignment, a corporate action) still
   lands in the journal carrying `needs_review = 1` and a note saying exactly
   what is wrong with it. A quiet skip is how a track record starts lying.

2. **Re-importing is safe.** Every imported trade stores a `broker_ref`
   fingerprint derived from the fills that built it, so overlapping exports —
   which is what you get when you pull "last 90 days" every month — dedupe
   instead of double-counting.

Parsing degrades rather than fails: the CSV path is stdlib-only, and the PDF
path asks for `pdfplumber`/`pypdf` only when it is actually handed a PDF.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import journal

# Robinhood's transaction codes, grouped by what they do to a position.
# SS/BC are the stock-side short codes: SS sells short (opens), BC buys to
# cover (closes) — found the hard way when 22 short-scalp fills vanished from
# a real export.
OPEN_LONG = {"BTO", "Buy"}
CLOSE_LONG = {"STC", "Sell"}
OPEN_SHORT = {"STO", "SS"}
CLOSE_SHORT = {"BTC", "BC"}
# Expiration and assignment/exercise close a position without a trade price.
EXPIRE = {"OEXP"}
ASSIGN = {"OASGN", "OEXCS", "OASGNX"}
TRADE_CODES = OPEN_LONG | CLOSE_LONG | OPEN_SHORT | CLOSE_SHORT | EXPIRE | ASSIGN

# "NVDA 8/15/2026 Call $180.00" — the option contract as Robinhood describes it.
OPTION_RE = re.compile(
    r"^(?P<sym>[A-Z][A-Z.\-]{0,9})\s+"
    r"(?P<mm>\d{1,2})/(?P<dd>\d{1,2})/(?P<yy>\d{2,4})\s+"
    r"(?P<kind>Call|Put)\s+"
    r"\$?(?P<strike>[\d,]+(?:\.\d+)?)\s*$",
    re.IGNORECASE,
)

MULT = {"call": 100, "put": 100, "stock": 1}


class ImportError_(ValueError):
    """Raised when a file cannot be read at all (bad format, missing library)."""


@dataclass
class Fill:
    """One broker-reported execution, normalized."""

    date: str                      # ISO date the activity happened
    code: str                      # BTO / STC / STO / BTC / OEXP / OASGN / Buy / Sell
    ticker: str
    instrument: str                # call | put | stock
    quantity: float
    price: float                   # per share (options: per share, not per contract)
    strike: float | None = None
    expiry: str | None = None      # ISO
    amount: float | None = None    # net cash, signed: negative = debit
    fees: float = 0.0
    raw: str = ""

    @property
    def key(self) -> tuple:
        """Contract identity — fills pair only within the same key."""
        return (self.ticker, self.instrument, self.strike, self.expiry)

    def fingerprint(self) -> str:
        return "|".join(str(x) for x in (
            self.date, self.code, self.ticker, self.instrument,
            self.strike, self.expiry, round(self.quantity, 4), round(self.price, 6)))


@dataclass
class Lot:
    """An open piece of a position, waiting for its exit."""

    quantity: float
    price: float
    date: str
    direction: str                 # long | short
    fees: float = 0.0
    fills: list[str] = field(default_factory=list)


# ---------- number / date helpers (statements are full of $ , ( ) noise) ----------

def _num(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s or s in {"-", "–", "N/A"}:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    # Robinhood suffixes share quantities on some rows ("10S", "2.5R").
    s = re.sub(r"[A-Za-z]+$", "", s).strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _iso(mm: str | int, dd: str | int, yy: str | int) -> str:
    y = int(yy)
    if y < 100:
        y += 2000
    return date(y, int(mm), int(dd)).isoformat()


def _parse_date(raw: str) -> str | None:
    s = (raw or "").strip()
    if not s:
        return None
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s)
    if m:
        try:
            return _iso(m.group(1), m.group(2), m.group(3))
        except ValueError:
            return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%b %d, %Y", "%d %b %Y"):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except ValueError:
            continue
    return None


# Expiration/assignment rows wrap the contract in prose:
# "Option Expiration for PLTR 8/7/2026 Put $165.00".
_CONTRACT_PREFIX_RE = re.compile(
    r"^Option\s+(?:Expiration|Assignment|Exercise)\s+for\s+", re.IGNORECASE)


def _parse_contract(description: str) -> tuple[str, str, float, str] | None:
    """('PLTR', 'put', 165.0, '2026-08-07') from an option description, else None."""
    cleaned = _CONTRACT_PREFIX_RE.sub("", (description or "").strip())
    m = OPTION_RE.match(cleaned)
    if not m:
        return None
    try:
        expiry = _iso(m.group("mm"), m.group("dd"), m.group("yy"))
    except ValueError:
        return None
    strike = _num(m.group("strike"))
    if strike is None:
        return None
    return m.group("sym").upper(), m.group("kind").lower(), strike, expiry


def _fees_for(amount: float | None, price: float, qty: float, instrument: str) -> float:
    """What the broker skimmed: the gap between notional and cash moved."""
    if amount is None:
        return 0.0
    notional = abs(price) * abs(qty) * MULT.get(instrument, 100)
    fee = abs(abs(amount) - notional)
    # A gap larger than 5% of notional is not a fee — it is a parse mismatch
    # (multi-leg row, adjusted contract). Don't invent a fee from it.
    return round(fee, 2) if notional and fee <= max(notional * 0.05, 5.0) else 0.0


# ---------- CSV: the primary path ----------

def parse_activity_csv(text: str) -> list[Fill]:
    """Robinhood 'Reports and Statements' activity CSV → fills.

    Non-trade rows (ACH transfers, dividends, interest, Gold fees) are ignored
    by design: they are cash movements, not positions.
    """
    text = text.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise ImportError_("that CSV has no header row")

    cols = {re.sub(r"[^a-z]", "", (c or "").lower()): c for c in reader.fieldnames}

    def col(row: dict, *names: str) -> str:
        for n in names:
            if n in cols:
                v = row.get(cols[n])
                if v not in (None, ""):
                    return str(v)
        return ""

    required = {"transcode", "quantity"}
    if not required <= set(cols):
        raise ImportError_(
            "this does not look like a Robinhood activity CSV — expected "
            "'Trans Code' and 'Quantity' columns, got: "
            + ", ".join(reader.fieldnames))

    fills: list[Fill] = []
    for row in reader:
        code = col(row, "transcode").strip()
        if code not in TRADE_CODES:
            continue
        when = _parse_date(col(row, "activitydate", "processdate", "settledate", "date"))
        if not when:
            continue
        description = col(row, "description")
        ticker = (col(row, "instrument", "symbol", "ticker") or "").upper().strip()

        contract = _parse_contract(description)
        if contract:
            sym, instrument, strike, expiry = contract
            ticker = ticker or sym
        else:
            instrument, strike, expiry = "stock", None, None
            if not ticker:
                continue  # a row with no symbol is not a position we can track

        qty = _num(col(row, "quantity"))
        price = _num(col(row, "price"))
        amount = _num(col(row, "amount"))
        if qty is None or qty == 0:
            continue
        if price is None:
            # Expirations and assignments legitimately have no price.
            if code in EXPIRE | ASSIGN:
                price = 0.0
            else:
                continue

        fills.append(Fill(
            date=when, code=code, ticker=ticker, instrument=instrument,
            quantity=abs(qty), price=abs(price), strike=strike, expiry=expiry,
            amount=amount,
            fees=_fees_for(amount, abs(price), abs(qty), instrument),
            raw=f"{when} {code} {description or ticker} {qty} @ {price}",
        ))
    return fills


# ---------- PDF statements: the fallback path ----------

def extract_pdf_text(path: str | Path) -> str:
    """Text out of a statement PDF, using whichever library is installed."""
    p = Path(path)
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(str(p)) as pdf:
            return "\n".join(page.extract_text() or "" for page in pdf.pages)
    except ModuleNotFoundError:
        pass
    try:
        from pypdf import PdfReader  # type: ignore

        return "\n".join(pg.extract_text() or "" for pg in PdfReader(str(p)).pages)
    except ModuleNotFoundError:
        raise ImportError_(
            "reading PDF statements needs a PDF library: "
            "pip install pdfplumber (or pypdf). The activity CSV export needs "
            "no extra libraries and parses more reliably — prefer it.")


# A statement line: date, description, code, qty, price, amount, in that order,
# with the description free to contain spaces.
STATEMENT_LINE_RE = re.compile(
    r"(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\s+"
    r"(?P<body>.+?)\s+"
    r"(?P<code>BTO|STC|STO|BTC|OEXP|OASGN|OEXCS|Buy|Sell)\s+"
    r"(?P<qty>[\d,]+(?:\.\d+)?)\s+"
    r"(?P<price>\$?[\d,]+(?:\.\d+)?|)\s*"
    r"(?P<amount>\(?\$?[\d,]+(?:\.\d+)?\)?|)\s*$"
)


def parse_statement_text(text: str) -> list[Fill]:
    """Best-effort fills out of statement text.

    Statement layouts drift between broker template versions, so this is
    deliberately forgiving: it scans every line for a trade-shaped pattern and
    ignores the rest, rather than assuming a fixed table geometry.
    """
    fills: list[Fill] = []
    for line in (text or "").splitlines():
        line = " ".join(line.split())
        if not line:
            continue
        m = STATEMENT_LINE_RE.search(line)
        if not m:
            continue
        when = _parse_date(m.group("date"))
        if not when:
            continue
        body = m.group("body").strip()
        code = m.group("code")
        qty = _num(m.group("qty"))
        price = _num(m.group("price"))
        amount = _num(m.group("amount"))
        if qty is None or qty == 0:
            continue

        contract = _parse_contract(body)
        if contract:
            ticker, instrument, strike, expiry = contract
        else:
            # Stock rows read "AAPL Apple Inc" or just "AAPL".
            sym = re.match(r"^([A-Z][A-Z.\-]{0,9})\b", body)
            if not sym:
                continue
            ticker, instrument, strike, expiry = sym.group(1), "stock", None, None
        if price is None:
            if code in EXPIRE | ASSIGN:
                price = 0.0
            else:
                continue

        fills.append(Fill(
            date=when, code=code, ticker=ticker, instrument=instrument,
            quantity=abs(qty), price=abs(price), strike=strike, expiry=expiry,
            amount=amount,
            fees=_fees_for(amount, abs(price), abs(qty), instrument),
            raw=line,
        ))
    return fills


def load_fills(path: str | Path | None = None, *, content: bytes | str | None = None,
               filename: str | None = None) -> list[Fill]:
    """Fills from a file on disk, or from raw bytes plus a filename hint."""
    name = (filename or (Path(path).name if path else "") or "").lower()

    if content is None:
        if path is None:
            raise ImportError_("nothing to import: pass a path or file content")
        p = Path(path).expanduser()
        if not p.exists():
            raise ImportError_(f"no such file: {p}")
        if p.suffix.lower() == ".pdf":
            return parse_statement_text(extract_pdf_text(p))
        content = p.read_bytes()

    if isinstance(content, bytes):
        if content[:5] == b"%PDF-":
            import tempfile

            with tempfile.NamedTemporaryFile(suffix=".pdf", delete=True) as tmp:
                tmp.write(content)
                tmp.flush()
                return parse_statement_text(extract_pdf_text(tmp.name))
        text = content.decode("utf-8", errors="replace")
    else:
        text = content

    if name.endswith(".csv") or "Trans Code" in text.split("\n", 1)[0]:
        return parse_activity_csv(text)
    # Unknown extension: try CSV, fall back to statement-text scanning.
    try:
        fills = parse_activity_csv(text)
        if fills:
            return fills
    except ImportError_:
        pass
    return parse_statement_text(text)


# ---------- fills → round-trip trades ----------

def _blank(fill: Fill) -> dict:
    return {
        "ticker": fill.ticker,
        "instrument": fill.instrument,
        "strike": fill.strike,
        "expiry": fill.expiry,
        "setup": "imported",
        "needs_review": 0,
        "review_note": None,
        "fees": 0.0,
    }


def _close(lot: Lot, fill: Fill, qty: float, note: str | None = None) -> dict:
    t = _blank(fill)
    t.update({
        "direction": lot.direction,
        "quantity": qty,
        "entry_price": lot.price,
        "entry_date": lot.date,
        "exit_price": fill.price,
        "exit_date": fill.date,
        "fees": round(lot.fees * (qty / lot.quantity if lot.quantity else 1)
                      + fill.fees * (qty / fill.quantity if fill.quantity else 1), 2),
        "fills": lot.fills + [fill.fingerprint()],
    })
    if note:
        t["needs_review"] = 1
        t["review_note"] = note
    return t


def match_fills(fills: list[Fill]) -> list[dict]:
    """Pair fills into trades, FIFO within each contract.

    FIFO because that is what the broker reports for tax lots, and because any
    other choice would need information the statement does not contain.
    """
    trades: list[dict] = []
    books: dict[tuple, list[Lot]] = {}

    # The activity CSV carries dates, not timestamps. Within a day the only
    # safe assumption for a day trader is that positions open before they
    # close — sorting opens first lets same-day round trips (including SS→BC
    # short scalps) pair instead of producing an orphan close and a
    # never-closed open.
    def _order(f: Fill) -> tuple:
        return (f.date, f.expiry or "", 0 if f.code in OPEN_LONG | OPEN_SHORT else 1)

    for fill in sorted(fills, key=_order):
        book = books.setdefault(fill.key, [])

        if fill.code in OPEN_LONG | OPEN_SHORT:
            book.append(Lot(
                quantity=fill.quantity, price=fill.price, date=fill.date,
                direction="long" if fill.code in OPEN_LONG else "short",
                fees=fill.fees, fills=[fill.fingerprint()]))
            continue

        # A closing fill: eat the oldest matching lots first.
        want = "long" if fill.code in CLOSE_LONG else "short"
        expiring = fill.code in EXPIRE | ASSIGN
        note = None
        if fill.code in EXPIRE:
            note = None  # expiring worthless is a normal, fully-known outcome
        elif fill.code in ASSIGN:
            note = ("assigned/exercised — booked at $0 on the option; the share "
                    "leg is not in this export, confirm the real outcome")

        remaining = fill.quantity
        while remaining > 1e-9 and book:
            lot = next((l for l in book if expiring or l.direction == want), None)
            if lot is None:
                break
            take = min(lot.quantity, remaining)
            trades.append(_close(lot, fill, take, note))
            remaining -= take
            lot.quantity -= take
            if lot.quantity <= 1e-9:
                book.remove(lot)

        if remaining > 1e-9:
            # An exit with no entry: the position was opened before this export
            # began. Book it flat so it cannot fake a win, and flag it loudly.
            orphan = _blank(fill)
            orphan.update({
                "direction": "long" if fill.code in CLOSE_LONG else "short",
                "quantity": remaining,
                "entry_price": fill.price,
                "entry_date": fill.date,
                "exit_price": fill.price,
                "exit_date": fill.date,
                "fees": round(fill.fees * (remaining / fill.quantity if fill.quantity else 1), 2),
                "needs_review": 1,
                "review_note": (f"closing fill ({fill.code}) with no matching entry in this "
                                f"export — booked flat at {fill.price}; set the real entry "
                                "price, or import an earlier date range"),
                "fills": [fill.fingerprint()],
            })
            trades.append(orphan)

    # Whatever is still open stays open — unless it is an option whose expiry
    # already passed within this export's window. That contract cannot still be
    # open; the expiration row is just missing. Flag it rather than guess the
    # outcome (worthless? auto-exercised?) on the trader's behalf.
    last_seen = max((f.date for f in fills), default="")
    for key, book in books.items():
        for lot in book:
            expiry = key[3]
            stale = bool(expiry) and expiry < last_seen
            t = {
                "ticker": key[0], "instrument": key[1], "strike": key[2], "expiry": expiry,
                "direction": lot.direction, "quantity": lot.quantity,
                "entry_price": lot.price, "entry_date": lot.date,
                "exit_price": None, "exit_date": None,
                "setup": "imported", "fees": lot.fees,
                "needs_review": 1 if stale else 0,
                "review_note": (f"expired {expiry} with no closing fill in this export — "
                                "close it at the real outcome ($0 if it expired worthless)"
                                if stale else None),
                "fills": lot.fills,
            }
            trades.append(t)

    trades.sort(key=lambda t: (t["entry_date"], t["ticker"]))
    for t in trades:
        t["broker_ref"] = hashlib.sha1(
            "|".join(sorted(t.pop("fills"))).encode()).hexdigest()[:20]
    return trades


# ---------- writing to the journal ----------

def _pnl_of(t: dict) -> float:
    sign = 1 if t["direction"] == "long" else -1
    mult = MULT.get(t["instrument"], 100)
    gross = sign * ((t["exit_price"] or 0) - t["entry_price"]) * t["quantity"] * mult
    return round(gross - (t.get("fees") or 0.0), 2)


def import_fills(fills: list[Fill], user_id: int, *, dry_run: bool = False,
                 source: str = "robinhood") -> dict:
    """Match fills and write the new trades to the journal.

    Returns a summary the CLI and the UI both render, including a
    `skipped_duplicates` count — re-running an overlapping export is expected,
    not an error.
    """
    journal.init()
    trades = match_fills(fills)

    existing = journal.existing_broker_refs(user_id)
    fresh = [t for t in trades if t["broker_ref"] not in existing]
    duplicates = len(trades) - len(fresh)

    written = 0
    if not dry_run:
        for t in fresh:
            journal.add_trade({**t, "source": source}, user_id)
            written += 1

    closed = [t for t in fresh if t["exit_price"] is not None]
    for t in fresh:  # so a preview can show the P&L without recomputing it
        t["pnl"] = _pnl_of(t) if t["exit_price"] is not None else None
    return {
        "fills_parsed": len(fills),
        "trades_found": len(trades),
        "new_trades": len(fresh),
        "written": written,
        "skipped_duplicates": duplicates,
        "closed": len(closed),
        "open": len(fresh) - len(closed),
        "needs_review": sum(1 for t in fresh if t["needs_review"]),
        "net_pnl": round(sum(_pnl_of(t) for t in closed), 2),
        "total_fees": round(sum(t.get("fees") or 0.0 for t in fresh), 2),
        "date_range": ([min(f.date for f in fills), max(f.date for f in fills)]
                       if fills else None),
        "tickers": sorted({t["ticker"] for t in fresh}),
        "dry_run": dry_run,
        "trades": fresh,
    }


def import_file(path: str | Path | None = None, *, user_id: int,
                content: bytes | str | None = None, filename: str | None = None,
                dry_run: bool = False) -> dict:
    fills = load_fills(path, content=content, filename=filename)
    if not fills:
        raise ImportError_(
            "no trade rows found in that file. If it is a Robinhood export, make "
            "sure it is the account activity report (the one with a 'Trans Code' "
            "column) and that the date range actually contains trades.")
    return import_fills(fills, user_id, dry_run=dry_run,
                        source=f"robinhood:{filename or (Path(path).name if path else 'upload')}")
