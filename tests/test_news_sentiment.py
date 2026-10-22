"""Headline sentiment tests.

These exist because of a live misread: the Analyst mailed the Trader a
"Positive news cluster: GOOGL" built from three bearish headlines about
executives leaving and the stock dropping. The stems were bare — \\bdrop\\b
cannot match "drops" — so present-tense headlines, which is nearly all of
them, scored neutral.

The failure direction is what makes this worth pinning down. A missed
negative does not produce a wrong alert; it produces *no* alert, and the
autonomous routine treats "no high-priority alert" as permission to trade.
An untested regex here quietly removes a safety check.
"""

from __future__ import annotations

import unittest

from app import news


class TestInflection(unittest.TestCase):
    """Headlines are present tense. Bare stems miss them."""

    def test_third_person_verbs_are_caught(self):
        for h in ("Stock drops on weak guidance",
                  "Tesla falls after delivery miss",
                  "Google loses another executive",
                  "Nvidia surges on record revenue",
                  "Broadcom jumps 8%"):
            self.assertNotEqual(news._sentiment(h), "neutral", h)

    def test_past_and_progressive_forms(self):
        self.assertEqual(news._sentiment("Shares plunged after the warning"), "negative")
        self.assertEqual(news._sentiment("Stock is climbing on strong demand"), "positive")
        self.assertEqual(news._sentiment("Chip maker dropped its forecast"), "negative")

    def test_multiword_phrases(self):
        for h in ("CEO is stepping down after ten years",
                  "Founder steps down from the board"):
            self.assertEqual(news._sentiment(h), "negative", h)


class TestTheRealMisread(unittest.TestCase):
    """The exact headlines that produced the bogus positive cluster."""

    HEADLINES = [
        "Alphabet’s stock drops as Google loses another key AI executive",
        "Jeff Dean and other top AI researchers are leaving Google to launch their own startup",
        "Google DeepMind CEO Demis Hassabis is stepping down, but will remain with the company",
    ]

    def test_cluster_is_not_positive(self):
        got = [news._sentiment(h) for h in self.HEADLINES]
        self.assertEqual(got.count("positive"), 0,
                         f"bearish headlines scored positive: {got}")

    def test_two_of_three_read_negative(self):
        got = [news._sentiment(h) for h in self.HEADLINES]
        self.assertGreaterEqual(got.count("negative"), 2, got)


class TestAmbiguousWordsExcluded(unittest.TestCase):
    """Words that carry no reliable signal must not vote."""

    def test_top_as_adjective_is_not_positive(self):
        # "top researchers" = leading, not "tops estimates".
        self.assertEqual(news._sentiment("Top engineers join the project"), "neutral")

    def test_tops_as_verb_still_counts(self):
        self.assertEqual(news._sentiment("Apple tops estimates"), "positive")

    def test_neutral_headline_stays_neutral(self):
        for h in ("Chipmaker announces new packaging process",
                  "Company schedules its annual developer conference"):
            self.assertEqual(news._sentiment(h), "neutral", h)


class TestScoringShape(unittest.TestCase):
    def test_mixed_headline_resolves_by_count(self):
        # One positive stem, two negative -> negative.
        self.assertEqual(
            news._sentiment("Revenue growth fails to offset weak margins and falling users"),
            "negative")

    def test_sentiment_only_returns_known_labels(self):
        for h in ("anything at all", "Stock surges", "Stock plunges"):
            self.assertIn(news._sentiment(h), {"positive", "negative", "neutral"})

    def test_sentiment_details_returns_matched_words(self):
        label, words = news._sentiment_details("SoFi surges after earnings beat estimates")
        self.assertEqual(label, "positive")
        self.assertIn("surges", words)
        self.assertIn("beat", words)


class TestWatchlistTickerTagging(unittest.TestCase):
    """Every ticker on the 17-ticker watchlist must be recognized in headlines."""

    def test_affordable_and_etf_tickers_are_tagged(self):
        cases = [
            ("SoFi Technologies rallies on strong loan growth", "SOFI"),
            ("Intel delays Ohio chip factory timeline", "INTC"),
            ("Rivian halts assembly line for retooling", "RIVN"),
            ("Ford Motor reports strong hybrid truck sales", "F"),
            ("Russell 2000 small caps jump as yields ease", "IWM"),
            ("S&P 500 hits new record high on tech rally", "SPY"),
            ("Nasdaq 100 surges ahead of CPI report", "QQQ"),
        ]
        for headline, expected_ticker in cases:
            tagged = news._tag_tickers(headline)
            self.assertIn(expected_ticker, tagged, f"{expected_ticker} missed in: {headline}")

    def test_rss_and_atom_xml_parsing_works_without_feedparser(self):
        sample_rss = b"""<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
          <item>
            <title>SoFi surges 8% after record member growth</title>
            <link>https://example.com/sofi</link>
            <pubDate>Wed, 21 Oct 2026 14:30:00 GMT</pubDate>
            <category>SOFI</category>
          </item>
        </channel></rss>"""
        entries = news._parse_xml_entries(sample_rss, limit=5)
        self.assertEqual(len(entries), 1)
        self.assertIn("SoFi surges", entries[0]["title"])
        item = news._build_news_item(
            source="TestFeed",
            title=entries[0]["title"],
            link=entries[0]["link"],
            ts=entries[0]["published"],
            categories=entries[0]["categories"],
        )
        self.assertEqual(item["sentiment"], "positive")
        self.assertIn("SOFI", item["tickers"])
        self.assertIn("surges", item["sentiment_words"])


if __name__ == "__main__":
    unittest.main()
