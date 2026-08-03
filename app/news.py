"""Live news aggregation.

Pulls technology / markets headlines from many reputable RSS feeds in
parallel, merges + dedupes them, and tags naive headline sentiment so the
UI can color-code. Per-ticker news comes from Yahoo Finance.
"""

from __future__ import annotations

import calendar
import concurrent.futures
import re
import threading
import time

import feedparser
import yfinance as yf

TECH_FEEDS = {
    "CNBC Technology": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
    "CNBC Markets": "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
    "TechCrunch": "https://techcrunch.com/feed/",
    "The Verge": "https://www.theverge.com/rss/index.xml",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "Wired": "https://www.wired.com/feed/rss",
    "MarketWatch Top": "https://feeds.content.dowjones.io/public/rss/mw_topstories",
    "MarketWatch Markets": "https://feeds.content.dowjones.io/public/rss/mw_marketpulse",
    "Yahoo Finance": "https://finance.yahoo.com/news/rssindex",
    "Investing.com Tech": "https://www.investing.com/rss/news_285.rss",
    "Seeking Alpha Tech": "https://seekingalpha.com/api/sa/combined/technology.xml",
    "Engadget": "https://www.engadget.com/rss.xml",
}

POSITIVE = re.compile(
    r"\b(surge|soar|jump|rall(y|ies)|beat|record|upgrade|bullish|gain|rise|"
    r"outperform|breakthrough|strong|growth|profit|top|boost|win)\b", re.I)
NEGATIVE = re.compile(
    r"\b(plunge|crash|fall|drop|tumble|miss|downgrade|bearish|loss|lawsuit|"
    r"layoff|cut|weak|slump|decline|recall|probe|fine|warn|fear|sell-?off)\b", re.I)

_cache: dict[str, tuple[float, list]] = {}
_lock = threading.Lock()

# Ticker tagging: match watchlist company names/symbols in headlines.
TICKER_PATTERNS = {
    "AAPL": r"\b(apple|aapl)\b",
    "MSFT": r"\b(microsoft|msft)\b",
    "NVDA": r"\b(nvidia|nvda)\b",
    "GOOGL": r"\b(google|alphabet|googl?)\b",
    "AMZN": r"\b(amazon|amzn|aws)\b",
    "META": r"\b(meta\b|facebook|instagram|whatsapp)",
    "TSLA": r"\b(tesla|tsla)\b",
    "AMD": r"\bamd\b",
    "AVGO": r"\b(broadcom|avgo)\b",
    "PLTR": r"\b(palantir|pltr)\b",
}
_TICKER_RE = {t: re.compile(p, re.I) for t, p in TICKER_PATTERNS.items()}


def _tag_tickers(title: str) -> list[str]:
    return [t for t, rx in _TICKER_RE.items() if rx.search(title)]


def _sentiment(title: str) -> str:
    pos = len(POSITIVE.findall(title))
    neg = len(NEGATIVE.findall(title))
    if pos > neg:
        return "positive"
    if neg > pos:
        return "negative"
    return "neutral"


def _fetch_feed(name: str, url: str) -> list[dict]:
    try:
        parsed = feedparser.parse(url)
        items = []
        for e in parsed.entries[:20]:
            ts = None
            for key in ("published_parsed", "updated_parsed"):
                if getattr(e, key, None):
                    ts = calendar.timegm(getattr(e, key))
                    break
            title = e.get("title", "").strip()
            items.append({
                "source": name,
                "title": title,
                "link": e.get("link", ""),
                "published": ts,
                "published_str": time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts)) if ts else "",
                "sentiment": _sentiment(title),
                "tickers": _tag_tickers(title),
            })
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
        for f in concurrent.futures.as_completed(futures, timeout=30):
            try:
                items.extend(f.result())
            except Exception:
                pass

    seen: set[str] = set()
    deduped = []
    for it in sorted(items, key=lambda x: -(x["published"] or 0)):
        key = it["title"].lower()[:80]
        if it["title"] and key not in seen:
            seen.add(key)
            deduped.append(it)

    with _lock:
        _cache["tech"] = (time.time(), deduped)
    return deduped[:limit]


def get_ticker_news(ticker: str, limit: int = 15) -> list[dict]:
    """Headlines for a specific ticker via Yahoo Finance (cached 3 min)."""
    key = f"tk:{ticker.upper()}"
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < 180:
            return hit[1][:limit]

    items = []
    try:
        for n in (yf.Ticker(ticker).news or [])[:limit]:
            content = n.get("content", n)
            title = content.get("title", "")
            url = (content.get("canonicalUrl") or {}).get("url") if isinstance(
                content.get("canonicalUrl"), dict) else content.get("link", "")
            pub = content.get("pubDate") or content.get("providerPublishTime")
            items.append({
                "source": (content.get("provider") or {}).get("displayName", "Yahoo Finance")
                if isinstance(content.get("provider"), dict) else "Yahoo Finance",
                "title": title,
                "link": url or "",
                "published_str": str(pub or ""),
                "sentiment": _sentiment(title),
            })
    except Exception:
        pass

    with _lock:
        _cache[key] = (time.time(), items)
    return items[:limit]
