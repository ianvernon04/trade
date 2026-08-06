"""Headline sentiment read by a model instead of counted by a word list.

`news._sentiment` counts positive and negative words in a title. That is fast
and free, and it is wrong in the cases that move money: "MU beats, stock
plunges" and "MU plunges despite beat" carry the same words and opposite
meanings, "analyst cuts target, keeps Buy" reads negative to a word list, and
"not the disaster feared" reads negative because of the word *disaster*.
Negation, contrast, and priced-in expectations are exactly what a keyword
counter cannot see, and the Analyst's whole job is reading headlines.

Two things shape the design:

- **Rules detect, the model interprets.** The deterministic pass already tags
  tickers and macro topics. Only headlines it flagged are worth a model's
  attention, so only those are sent — the scan stays cheap and the model is
  spent on the rows that reach the Trader's inbox.
- **The rules stay underneath.** Every failure here — no SDK, no credentials,
  an API error, a malformed reply, a row the model didn't return — falls back
  to the word-list verdict for that headline rather than dropping it or
  guessing. A scan that can't reach the model is a scan with slightly worse
  sentiment, never a scan with no news.

Off unless ``ANALYST_LLM_SENTIMENT=1``. The Analyst runs every 15 minutes as a
daemon thread; enabling it by merely detecting a key would turn an API key
added for something else into ~96 unrequested calls a day.
"""

from __future__ import annotations

import json
import os

MODEL = "claude-opus-5"
ENV_FLAG = "ANALYST_LLM_SENTIMENT"

# Bounds the spend of a single scan. Headlines past this keep the word-list
# verdict; the count is reported so a truncated scan is never silent.
MAX_HEADLINES = 60

_VALID = ("positive", "negative", "neutral")

_SCHEMA = {
    "type": "object",
    "properties": {
        "ratings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "sentiment": {"type": "string", "enum": list(_VALID)},
                },
                "required": ["i", "sentiment"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["ratings"],
    "additionalProperties": False,
}

SYSTEM = (
    "You rate financial news headlines for a trading desk. For each numbered "
    "headline, decide what it means for the mentioned company's share price.\n\n"
    "Rate the market implication, not the vocabulary. A headline can contain "
    "grim words and be good news, or upbeat words and be bad news:\n"
    "- Judge the outcome against expectations. A beat that sends the stock "
    "down is negative; a smaller-than-feared loss is positive.\n"
    "- Read contrast and negation. 'Beats, but guides lower' is negative; "
    "'not the collapse analysts feared' is positive.\n"
    "- A downgrade that still leaves a Buy rating is milder than an outright "
    "cut to Sell; weigh what actually changed.\n"
    "- Neutral is a real answer. Use it for routine coverage, scheduling "
    "notices, and pieces with no directional read — do not force a call.\n\n"
    "Return one rating per headline, using the index you were given."
)


def enabled() -> bool:
    return os.environ.get(ENV_FLAG, "").strip() in ("1", "true", "yes", "on")


def _candidates(items: list[dict]) -> list[int]:
    """Positions the deterministic pass already found interesting.

    A headline with no ticker and no macro hit never reaches the Trader, so
    paying a model to re-read it buys nothing.
    """
    from .newsagent import _MACRO_RE

    out = []
    for i, it in enumerate(items):
        if it.get("tickers"):
            out.append(i)
            continue
        title = it.get("title") or ""
        if any(rx.search(title) for rx in _MACRO_RE.values()):
            out.append(i)
    return out


def _client():
    """The SDK is optional; the Analyst predates it and still runs without it."""
    import anthropic

    return anthropic.Anthropic()


def rescore(items: list[dict], client=None) -> tuple[list[dict], dict]:
    """Re-rate the headlines worth re-rating. Never raises.

    Returns the items (a new list; inputs are not mutated) and a meta dict
    saying which path actually ran, so the caller can log what happened rather
    than assume the model was consulted.
    """
    items = [dict(it) for it in items]
    meta = {"path": "rules", "reason": None, "rated": 0,
            "considered": 0, "skipped_over_cap": 0, "model": MODEL}

    if not enabled():
        meta["reason"] = f"{ENV_FLAG} not set"
        return items, meta

    idx = _candidates(items)
    meta["considered"] = len(idx)
    if not idx:
        meta["reason"] = "no headline mentioned a ticker or macro topic"
        return items, meta

    if len(idx) > MAX_HEADLINES:
        meta["skipped_over_cap"] = len(idx) - MAX_HEADLINES
        idx = idx[:MAX_HEADLINES]

    numbered = "\n".join(f"{i}. {items[i].get('title', '')}" for i in idx)

    try:
        client = client or _client()
        resp = client.messages.create(
            model=MODEL,
            max_tokens=8000,  # thinking is on by default and shares this cap
            system=SYSTEM,
            output_config={
                "effort": "low",  # classification; low is strong here and cheap
                "format": {"type": "json_schema", "schema": _SCHEMA},
            },
            messages=[{"role": "user", "content": numbered}],
        )
    except Exception as exc:  # noqa: BLE001 — every failure degrades to rules
        meta["reason"] = f"{type(exc).__name__}: {exc}"
        return items, meta

    if getattr(resp, "stop_reason", None) == "refusal":
        meta["reason"] = "model declined to rate this batch"
        return items, meta

    try:
        text = next(b.text for b in resp.content if b.type == "text")
        ratings = json.loads(text)["ratings"]
    except Exception as exc:  # noqa: BLE001 — a malformed reply is not fatal
        meta["reason"] = f"unreadable response: {type(exc).__name__}: {exc}"
        return items, meta

    allowed = set(idx)
    rated = 0
    for r in ratings:
        i, s = r.get("i"), r.get("sentiment")
        # Only positions we actually sent, and only verdicts we recognize —
        # an out-of-range index would otherwise rewrite an unrelated headline.
        if isinstance(i, int) and i in allowed and s in _VALID:
            items[i]["sentiment"] = s
            items[i]["sentiment_source"] = "llm"
            rated += 1

    meta["rated"] = rated
    if rated:
        meta["path"] = "llm"
        if rated < len(idx):
            # Unreturned rows keep the word-list verdict; say so rather than
            # letting a partial answer read as a complete one.
            meta["reason"] = f"{len(idx) - rated} headline(s) unrated, kept word-list verdict"
    else:
        meta["reason"] = "model returned no usable ratings"
    return items, meta
