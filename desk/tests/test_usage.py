"""使える残り（Claude の Pro 枠・Gemini の無料枠）"""

import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path

from ray import local, usage
from ray.ui import StateHub
from tests.test_local import FakeGeminiHttp, gemini


class FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class ClaudeUsageTest(unittest.TestCase):
    def test_reads_left_percent(self):
        seen = {}

        def urlopen(req, timeout=None):
            seen["auth"] = req.get_header("Authorization")
            return FakeResp(json.dumps({
                "five_hour": {"utilization": 23.4, "resets_at": "2026-11-19T08:00:00+00:00"},
                "seven_day": {"utilization": 61.0, "resets_at": "2026-11-22T00:00:00+00:00"}}).encode())

        u = usage.claude_usage(urlopen=urlopen, token=lambda: "tok")
        self.assertEqual(seen["auth"], "Bearer tok")
        self.assertTrue(u["ok"])
        self.assertEqual(u["five_hour"]["left"], 77)
        self.assertEqual(u["seven_day"]["left"], 39)

    def test_no_token_or_error_is_unknown(self):
        self.assertFalse(usage.claude_usage(token=lambda: None)["ok"])

        def fail(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, None)

        u = usage.claude_usage(urlopen=fail, token=lambda: "tok")
        self.assertFalse(u["ok"])
        self.assertIn("401", u["why"])


class GeminiUsageTest(unittest.TestCase):
    def test_left_counts_down(self):
        g = gemini(FakeGeminiHttp([["はい。"]]))
        self.assertEqual(usage.gemini_usage(g)["left"], 450)
        g.ask([], "こんにちは", lambda t: None)
        self.assertEqual(usage.gemini_usage(g)["left"], 449)

    def test_429_shows_zero(self):
        g = gemini(FakeGeminiHttp([429]))
        g.ask([], "こんにちは", lambda t: None)
        self.assertEqual(usage.gemini_usage(g)["left"], 0)

    def test_search_429_keeps_plain_questions(self):
        # 検索つきだけ上限：ふつうの質問は Gemini のまま、調べものは Claude へ
        http = FakeGeminiHttp([429, ["はい。"]])
        g = gemini(http)
        self.assertIsNone(g.ask([], "天気を調べて", lambda t: None, search=True))
        self.assertTrue(g.available())
        self.assertFalse(g.search_available())
        self.assertIsNone(g.ask([], "もう一度調べて", lambda t: None, search=True))  # 呼ばずに Claude へ
        self.assertEqual(g.ask([], "こんにちは", lambda t: None), "はい。")
        u = usage.gemini_usage(g)
        self.assertEqual(u["left"], 448)
        self.assertFalse(u["search"])
        self.assertEqual(u["search_used"], 1)

    def test_search_limit_counts_down(self):
        g = gemini(FakeGeminiHttp([["晴れ。"]]))
        g.c = dict(g.c, search_daily_limit=2)
        g.ask([], "天気を調べて", lambda t: None, search=True)
        u = usage.gemini_usage(g)
        self.assertEqual((u["search_used"], u["search_limit"], u["search"]), (1, 2, True))
        g.search_count = 2
        self.assertFalse(g.search_available())  # 書いた上限の手前で止める

    def test_no_key(self):
        self.assertFalse(usage.gemini_usage(gemini(key=""))["ok"])

    def test_count_survives_restart_until_reset(self):
        with tempfile.TemporaryDirectory() as d:
            store = Path(d) / "g.json"
            noon = lambda: datetime(2026, 11, 19, 12, 0, tzinfo=local.JST)
            g = local.GeminiBrain(_cfg(), urlopen=FakeGeminiHttp([["はい。"]]),
                                  now=noon, env={"GEMINI_API_KEY": "k"}, store=store)
            g.ask([], "こんにちは", lambda t: None)
            again = local.GeminiBrain(_cfg(), now=noon, env={"GEMINI_API_KEY": "k"}, store=store)
            self.assertEqual(usage.gemini_usage(again)["left"], 449)
            evening = lambda: datetime(2026, 11, 19, 17, 30, tzinfo=local.JST)
            later = local.GeminiBrain(_cfg(), now=evening, env={"GEMINI_API_KEY": "k"}, store=store)
            self.assertEqual(usage.gemini_usage(later)["left"], 450)


def _cfg():
    from tests.test_local import cfg
    return cfg()


class WatcherTest(unittest.TestCase):
    def test_puts_usage_on_hub_and_caches_claude(self):
        hub = StateHub()
        calls = []
        t = [0.0]
        w = usage.UsageWatcher(hub, gemini=gemini(), fetch_claude=lambda: calls.append(1) or {"ok": False, "why": "x"},
                               clock=lambda: t[0])
        w.refresh()
        w.refresh()
        self.assertEqual(len(calls), 1)
        t[0] = 301
        w.refresh()
        self.assertEqual(len(calls), 2)
        self.assertEqual(hub.state["usage"]["gemini"]["left"], 450)


if __name__ == "__main__":
    unittest.main()
