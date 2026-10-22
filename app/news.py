"""Live news aggregation.

Pulls technology / markets headlines from many reputable RSS feeds in
parallel, merges + dedupes them, and tags naive headline sentiment so the
UI can color-code. Per-ticker news comes from Yahoo Finance.
"""

from __future__ import annotations

import calendar
import concurrent.futures
import email.utils
import html
import re
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

try:
    import feedparser  # type: ignore
except ImportError:
    feedparser = None

try:
    import requests  # type: ignore
except ImportError:
    requests = None

try:
    import yfinance as yf  # type: ignore
except ImportError:
    yf = None

TECH_FEEDS = {
    "CNBC Technology": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    "CNBC Markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "Wired": "https://www.wired.com/feed/rss",
    "MarketWatch Top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "MarketWatch Markets": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "Nasdaq Markets": "https://www.nasdaq.com/feed/rssoutbound?category=Markets",
    "Investing.com Tech": "https://www.investing.com/rss/news_285.rss",
    "Seeking Alpha": "https://seekingalpha.com/market_currents.xml",
    "Google Finance News": "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en",
    "Engadget": "https://www.engadget.com/rss.xml",
}

# Sentiment stems, inflected automatically. Headlines are written in the
# present tense — "Alphabet's stock drops", "Google loses an executive" — so
# matching only the bare stem missed most of them: \bdrop\b cannot match
# "drops", \bfall\b cannot match "falls". Every miss scored the headline
# neutral rather than negative, which is the dangerous direction here: the
# Analyst gates autonomous trading on negative clusters, so a miss silently
# removes a safety check rather than tripping one.
#
# Ambiguous stems are deliberately absent. "top" matched "top AI researchers"
# (leading) far more often than "tops estimates"; "fine" and "probe" have
# common non-financial senses. Where a word only carries signal inflected,
# the inflected form is listed on its own — "tops", not "top".
_POSITIVE_STEMS = [
    "surge", "soar", "jump", "rally", "rallies", "beat", "record", "upgrade",
    "bullish", "gain", "rise", "outperform", "breakthrough", "strong",
    "growth", "profit", "tops", "boost", "win", "climb", "advance", "expand",
]
_NEGATIVE_STEMS = [
    "plunge", "crash", "fall", "drop", "tumble", "miss", "downgrade",
    "bearish", "loss", "lose", "lawsuit", "layoff", "cut", "weak", "slump",
    "decline", "recall", "probe", "warn", "fear", "sink", "slide", "halt",
    "resign", "step down", "steps down", "stepping down", "depart", "exit",
    "sell-off", "selloff", "slash", "plummet", "downturn",
]

# (e)s / ed / ing, plus doubled-consonant forms (dropped, dropping) and
# y -> ies. Kept as one suffix group so a new stem needs no extra thought.
_INFLECT = r"(?:s|es|ed|d|ing|ped|ping|bed|bing|ned|ning|ged|ging)?"


def _stem_re(stems: list[str]) -> re.Pattern:
    parts = []
    for s in stems:
        if " " in s:                      # multi-word phrases: match verbatim
            parts.append(re.escape(s))
        else:
            parts.append(re.escape(s) + _INFLECT)
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.I)


POSITIVE = _stem_re(_POSITIVE_STEMS)
NEGATIVE = _stem_re(_NEGATIVE_STEMS)

_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()

# Ticker tagging: match all 17 watchlist company names/symbols in headlines.
TICKER_PATTERNS = {
    "AAPL": r"\b(apple|aapl|iphone|ipad|macbook)\b",
    "MSFT": r"\b(microsoft|msft|azure|copilot)\b",
    "NVDA": r"\b(nvidia|nvda|blackwell|geforce)\b",
    "GOOGL": r"\b(google|alphabet|googl?|deepmind|waymo|youtube)\b",
    "AMZN": r"\b(amazon|amzn|aws)\b",
    "META": r"\b(meta\b|facebook|instagram|whatsapp|threads)",
    "TSLA": r"\b(tesla|tsla|cybertruck)\b",
    "AMD": r"\b(amd|advanced micro devices|ryzen|epyc)\b",
    "AVGO": r"\b(broadcom|avgo)\b",
    "PLTR": r"\b(palantir|pltr)\b",
    "INTC": r"\b(intel|intc)\b",
    "SOFI": r"\b(sofi)\b",
    "F": r"\b(ford motor|ford's|ford stock|ford ev|\bford\b)\b",
    "RIVN": r"\b(rivian|rivn)\b",
    "SPY": r"\b(spy|s&p 500|s&p500)\b",
    "QQQ": r"\b(qqq|nasdaq-100|nasdaq 100)\b",
    "IWM": r"\b(iwm|russell 2000)\b",
}
_TICKER_RE = {t: re.compile(p, re.I) for t, p in TICKER_PATTERNS.items()}

TICKER_SEARCH_NAMES = {
    "AAPL": "Apple AAPL stock",
    "MSFT": "Microsoft MSFT stock",
    "NVDA": "Nvidia NVDA stock",
    "GOOGL": "Alphabet Google GOOGL stock",
    "AMZN": "Amazon AMZN stock",
    "META": "Meta Platforms META stock",
    "TSLA": "Tesla TSLA stock",
    "AMD": "AMD Advanced Micro Devices stock",
    "AVGO": "Broadcom AVGO stock",
    "PLTR": "Palantir PLTR stock",
    "INTC": "Intel INTC stock",
    "SOFI": "SoFi Technologies SOFI stock",
    "F": "Ford Motor F stock",
    "RIVN": "Rivian RIVN stock",
    "SPY": "SPY S&P 500 ETF",
    "QQQ": "QQQ Nasdaq 100 ETF",
    "IWM": "IWM Russell 2000 ETF",
}


def _tag_tickers(title: str, extra_categories: list[str] | None = None) -> list[str]:
    matched = [t for t, rx in _TICKER_RE.items() if rx.search(title)]
    if extra_categories:
        for cat in extra_categories:
            up = cat.strip().upper()
            if up in TICKER_PATTERNS and up not in matched:
                matched.append(up)
    return matched


def _sentiment_details(title: str) -> tuple[str, list[str]]:
    pos_hits = [m.lower() for m in POSITIVE.findall(title)]
    neg_hits = [m.lower() for m in NEGATIVE.findall(title)]
    if len(pos_hits) > len(neg_hits):
        return "positive", list(dict.fromkeys(pos_hits))
    if len(neg_hits) > len(pos_hits):
        return "negative", list(dict.fromkeys(neg_hits))
    return "neutral", list(dict.fromkeys(pos_hits + neg_hits))


def _sentiment(title: str) -> str:
    label, _ = _sentiment_details(title)
    return label


# A feed that accepts the connection and then never sends a byte must not be
# able to wedge the fetch forever.
FEED_TIMEOUT = (5, 10)  # (connect, read) seconds
_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _http_get_bytes(url: str) -> bytes:
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    if requests is not None:
        resp = requests.get(url, timeout=FEED_TIMEOUT, headers=headers)
        resp.raise_for_status()
        return resp.content
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=FEED_TIMEOUT[1]) as r:
        return r.read()


def _parse_date_to_epoch(raw: str | None) -> int | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        dt = email.utils.parsedate_to_datetime(raw)
        return int(dt.timestamp())
    except Exception:
        pass
    # ISO-8601 fallback (e.g. Atom <updated>2026-10-21T14:22:00Z</updated>)
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S"):
        try:
            return int(calendar.timegm(time.strptime(raw[:19], fmt[:17])))
        except Exception:
            continue
    return None


def _parse_xml_entries(content: bytes, limit: int = 20) -> list[dict]:
    """Zero-dependency RSS 2.0 / Atom XML parser used when feedparser is absent."""
    if feedparser is not None:
        parsed = feedparser.parse(content)
        out = []
        for e in parsed.entries[:limit]:
            ts = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(e, key, None):
                    ts = calendar.timegm(getattr(e, key))
                    break
            if ts is None:
                ts = _parse_date_to_epoch(e.get("published") or e.get("updated"))
            cats = [t.get("term", "") for t in (e.get("tags") or []) if isinstance(t, dict)]
            out.append({
                "title": html.unescape(e.get("title", "").strip()),
                "link": e.get("link", ""),
                "published": ts,
                "categories": cats,
            })
        return out

    root = ET.fromstring(content)
    out = []
    # RSS 2.0 <item>
    rss_items = root.findall(".//item")
    if rss_items:
        for it in rss_items[:limit]:
            title = html.unescape((it.findtext("title") or "").strip())
            link = (it.findtext("link") or "").strip()
            pub = (
                it.findtext("pubDate")
                or it.findtext("{http://purl.org/dc/elements/1.1/}date")
                or ""
            )
            cats = [c.text or "" for c in it.findall("category") if c.text]
            out.append({
                "title": title,
                "link": link,
                "published": _parse_date_to_epoch(pub),
                "categories": cats,
            })
        return out

    # Atom <entry>
    ns = "{http://www.w3.org/2005/Atom}"
    for entry in root.findall(f".//{ns}entry")[:limit]:
        title = html.unescape((entry.findtext(f"{ns}title") or "").strip())
        link = ""
        link_el = entry.find(f"{ns}link")
        if link_el is not None:
            link = link_el.attrib.get("href", "")
        pub = entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated") or ""
        cats = [c.attrib.get("term", "") for c in entry.findall(f"{ns}category")]
        out.append({
            "title": title,
            "link": link,
            "published": _parse_date_to_epoch(pub),
            "categories": cats,
        })
    return out


def _build_news_item(
    source: str,
    title: str,
    link: str,
    ts: int | None,
    published_str: str | None = None,
    categories: list[str] | None = None,
    forced_ticker: str | None = None,
) -> dict:
    # Google News RSS often appends " - Publisher Name" to the title
    clean_title = title.strip()
    inferred_source = source
    if source.startswith("Google ") and " - " in clean_title:
        parts = clean_title.rsplit(" - ", 1)
        if 2 <= len(parts[1]) <= 36:
            clean_title = parts[0].strip()
            inferred_source = parts[1].strip()

    sent_label, sent_words = _sentiment_details(clean_title)
    tickers = _tag_tickers(clean_title, categories)
    if forced_ticker and forced_ticker.upper() not in tickers:
        tickers.insert(0, forced_ticker.upper())

    pub_str = published_str
    if not pub_str:
        pub_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else ""

    return {
        "source": inferred_source,
        "title": clean_title,
        "link": link,
        "published": ts,
        "published_str": pub_str,
        "sentiment": sent_label,
        "sentiment_words": sent_words,
        "tickers": tickers,
    }


def _fetch_feed(name: str, url: str, forced_ticker: str | None = None, limit: int = 20) -> list[dict]:
    try:
        raw = _http_get_bytes(url)
        entries = _parse_xml_entries(raw, limit=limit)
        items = []
        for e in entries:
            if not e.get("title"):
                continue
            items.append(
                _build_news_item(
                    source=name,
                    title=e["title"],
                    link=e.get("link", ""),
                    ts=e.get("published"),
                    categories=e.get("categories"),
                    forced_ticker=forced_ticker,
                )
            )
        return items
    except Exception:
        return []


def get_tech_news(limit: int = 80) -> list[dict]:
    """Merged tech/market headlines from all feeds (cached 3 min)."""
    now = time.time()
    with _lock:
        hit = _cache.get("tech")
        if hit and now - hit[0] < 180:
            return hit[1][:limit]

    items: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_fetch_feed, n, u) for n, u in TECH_FEEDS.items()]
        try:
            for f in concurrent.futures.as_completed(futures, timeout=15):
                try:
                    items.extend(f.result())
                except Exception:
                    pass
        except concurrent.futures.TimeoutError:
            pass

    seen: set[str] = set()
    deduped = []
    for it in sorted(items, key=lambda x: -(x["published"] or 0)):
        key = re.sub(r"\W+", " ", it["title"].lower()).strip()[:75]
        if it["title"] and key not in seen:
            seen.add(key)
            deduped.append(it)

    if deduped:
        with _lock:
            _cache["tech"] = (time.time(), deduped)
    return deduped[:limit]


def get_ticker_news(ticker: str, limit: int = 15) -> list[dict]:
    """Headlines for a specific ticker via Yahoo Finance + Google News RSS fallback (cached 3 min)."""
    sym = ticker.strip().upper()
    key = f"tk:{sym}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < 180:
            return hit[1][:limit]

    items: list[dict] = []

    # 1. Try Yahoo Finance Ticker.news first when yfinance is available
    if yf is not None:
        try:
            for n in (yf.Ticker(sym).news or [])[:limit]:
                content = n.get("content", n) if isinstance(n, dict) else {}
                title = (content.get("title") or "").strip()
                if not title:
                    continue
                url = (
                    (content.get("canonicalUrl") or {}).get("url")
                    if isinstance(content.get("canonicalUrl"), dict)
                    else content.get("link", "")
                )
                pub = content.get("pubDate") or content.get("providerPublishTime")
                ts = None
                if isinstance(pub, (int, float)):
                    ts = int(pub)
                    pub_str = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
                else:
                    ts = _parse_date_to_epoch(str(pub or ""))
                    pub_str = (
                        time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts))
                        if ts
                        else str(pub or "")
                    )
                src = (
                    (content.get("provider") or {}).get("displayName", "Yahoo Finance")
                    if isinstance(content.get("provider"), dict)
                    else "Yahoo Finance"
                )
                items.append(
                    _build_news_item(
                        source=src,
                        title=title,
                        link=url or "",
                        ts=ts,
                        published_str=pub_str,
                        forced_ticker=sym,
                    )
                )
        except Exception:
            pass

    # 2. Resilient fallback / supplement: Google News RSS search for this ticker
    if len(items) < limit:
        q_term = TICKER_SEARCH_NAMES.get(sym, f"{sym} stock")
        q_encoded = urllib.parse.quote_plus(f"{q_term} when:7d")
        rss_url = f"https://news.google.com/rss/search?q={q_encoded}&hl=en-US&gl=US&ceid=US:en"
        items.extend(_fetch_feed("Google Finance", rss_url, forced_ticker=sym, limit=limit))

    # 3. Also pull any matching tagged headlines already in the broad tech/market cache
    with _lock:
        tech_cached = _cache.get("tech")
    if tech_cached:
        for it in tech_cached[1]:
            if sym in (it.get("tickers") or []):
                items.append(it)

    # Deduplicate while preserving chronological order
    seen: set[str] = set()
    deduped: list[dict] = []
    for it in sorted(items, key=lambda x: -(x.get("published") or 0)):
        k = re.sub(r"\W+", " ", it["title"].lower()).strip()[:75]
        if it["title"] and k not in seen:
            seen.add(k)
            deduped.append(it)

    if deduped:
        with _lock:
            _cache[key] = (time.time(), deduped)
    return deduped[:limit]
