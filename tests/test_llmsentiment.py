"""The model reads headlines; the word list stays underneath it.

Every test here injects a fake client — no network, no SDK, no credentials.
The failure paths matter more than the happy one: this runs unattended every
15 minutes, so the question that decides whether it is safe to ship is what
happens when the model is unreachable, slow, or wrong.
"""

from __future__ import annotations

import json
import os
import unittest

from app import llmsentiment


class _Block:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Resp:
    stop_reason = "end_turn"

    def __init__(self, text):
        self.content = [_Block(text)]


class _FakeClient:
    """Records the request and replays a canned response."""

    def __init__(self, payload=None, raises=None, stop_reason=None):
        self._payload = payload
        self._raises = raises
        self._stop_reason = stop_reason
        self.calls = []

    @property
    def messages(self):
        return self

    def create(self, **kw):
        self.calls.append(kw)
        if self._raises:
            raise self._raises
        r = _Resp(json.dumps(self._payload))
        if self._stop_reason:
            r.stop_reason = self._stop_reason
        return r


def _items():
    return [
        {"title": "MU beats, stock plunges on weak guidance",
         "tickers": ["MU"], "sentiment": "positive"},
        {"title": "Fed signals rate cut", "tickers": [], "sentiment": "neutral"},
        {"title": "A local bakery opened downtown", "tickers": [],
         "sentiment": "neutral"},
    ]


class TestDisabledByDefault(unittest.TestCase):
    def setUp(self):
        os.environ.pop(llmsentiment.ENV_FLAG, None)

    def test_off_unless_explicitly_enabled(self):
        """A key added for something else must not start billing this agent."""
        client = _FakeClient({"ratings": [{"i": 0, "sentiment": "negative"}]})
        items, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(meta["path"], "rules")
        self.assertEqual(client.calls, [], "no model call while disabled")
        self.assertEqual(items[0]["sentiment"], "positive")


class _EnabledCase(unittest.TestCase):
    def setUp(self):
        os.environ[llmsentiment.ENV_FLAG] = "1"

    def tearDown(self):
        os.environ.pop(llmsentiment.ENV_FLAG, None)


class TestRescoring(_EnabledCase):
    def test_model_verdict_overrides_the_word_list(self):
        """'beats … plunges' is what the keyword counter gets wrong."""
        client = _FakeClient({"ratings": [
            {"i": 0, "sentiment": "negative"},
            {"i": 1, "sentiment": "positive"},
        ]})
        items, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(meta["path"], "llm")
        self.assertEqual(items[0]["sentiment"], "negative")
        self.assertEqual(items[0]["sentiment_source"], "llm")

    def test_only_flagged_headlines_are_sent(self):
        """The bakery has no ticker and no macro hit — it never reaches the inbox."""
        client = _FakeClient({"ratings": []})
        llmsentiment.rescore(_items(), client=client)
        sent = client.calls[0]["messages"][0]["content"]
        self.assertIn("MU beats", sent)
        self.assertIn("Fed signals", sent)   # macro pattern
        self.assertNotIn("bakery", sent)

    def test_untouched_headlines_keep_the_word_list_verdict(self):
        client = _FakeClient({"ratings": [{"i": 0, "sentiment": "negative"}]})
        items, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(items[1]["sentiment"], "neutral")
        self.assertNotIn("sentiment_source", items[1])
        self.assertIn("unrated", meta["reason"])

    def test_inputs_are_not_mutated(self):
        original = _items()
        client = _FakeClient({"ratings": [{"i": 0, "sentiment": "negative"}]})
        llmsentiment.rescore(original, client=client)
        self.assertEqual(original[0]["sentiment"], "positive")

    def test_uses_the_configured_model(self):
        client = _FakeClient({"ratings": []})
        llmsentiment.rescore(_items(), client=client)
        self.assertEqual(client.calls[0]["model"], "claude-opus-5")


class TestDegradation(_EnabledCase):
    """Every one of these is a live failure mode at 15-minute cadence."""

    def test_api_error_falls_back_to_rules(self):
        client = _FakeClient(raises=RuntimeError("connection reset"))
        items, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(meta["path"], "rules")
        self.assertIn("connection reset", meta["reason"])
        self.assertEqual(items[0]["sentiment"], "positive")

    def test_malformed_response_falls_back_to_rules(self):
        client = _FakeClient({"wrong_key": []})
        items, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(meta["path"], "rules")
        self.assertIn("unreadable", meta["reason"])
        self.assertEqual(items[0]["sentiment"], "positive")

    def test_refusal_falls_back_to_rules(self):
        client = _FakeClient({"ratings": []}, stop_reason="refusal")
        _, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(meta["path"], "rules")
        self.assertIn("declined", meta["reason"])

    def test_out_of_range_index_is_ignored(self):
        """A stray index must not rewrite a headline that was never sent."""
        client = _FakeClient({"ratings": [
            {"i": 2, "sentiment": "negative"},    # the bakery — not sent
            {"i": 99, "sentiment": "negative"},   # nonexistent
        ]})
        items, meta = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(items[2]["sentiment"], "neutral")
        self.assertEqual(meta["path"], "rules")

    def test_unrecognized_verdict_is_ignored(self):
        client = _FakeClient({"ratings": [{"i": 0, "sentiment": "very bullish"}]})
        items, _ = llmsentiment.rescore(_items(), client=client)
        self.assertEqual(items[0]["sentiment"], "positive")

    def test_batch_cap_is_reported_not_silent(self):
        many = [{"title": f"NVDA story {i}", "tickers": ["NVDA"],
                 "sentiment": "neutral"} for i in range(llmsentiment.MAX_HEADLINES + 5)]
        client = _FakeClient({"ratings": []})
        _, meta = llmsentiment.rescore(many, client=client)
        self.assertEqual(meta["skipped_over_cap"], 5)
        self.assertEqual(meta["considered"], llmsentiment.MAX_HEADLINES + 5)

    def test_no_candidates_skips_the_call_entirely(self):
        client = _FakeClient({"ratings": []})
        items = [{"title": "A local bakery opened", "tickers": [],
                  "sentiment": "neutral"}]
        _, meta = llmsentiment.rescore(items, client=client)
        self.assertEqual(client.calls, [])
        self.assertEqual(meta["path"], "rules")


if __name__ == "__main__":
    unittest.main()
