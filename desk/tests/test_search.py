"""検索の受け皿：検索 API（Tavily）→ Gemini 無料枠で要約 → Claude"""

import json
import tempfile
import unittest
import urllib.error
from datetime import datetime
from pathlib import Path

from ray import cmdlog, config, local, search
from ray.app import Assistant
from ray.ui import StateHub
from tests.test_local import FakeClaude, FakeResp, chat_stream, sse

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=local.JST)
KEY = "tvly-secret-key"
RAMEN = [
    {"title": "吉村家 - 横浜家系ラーメンの総本山", "url": "https://example.com/yoshimuraya", "content": "横浜駅西口から徒歩8分。家系ラーメンの元祖、吉村家。"},
    {"title": "杉田家 | 家系ラーメン", "url": "https://example.jp/sugitaya", "content": "新杉田駅すぐ。朝5時から営業する家系の名店、杉田家。"},
]


class FakeNet:
    """urlopen の代わり。Tavily・Gemini・Ollama に答える"""

    def __init__(self, tavily=None, gemini=None, ollama=None):
        self.tavily = list(tavily or [])
        self.gemini = list(gemini or [])
        self.ollama = list(ollama or [])
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        body = json.loads(req.data) if not isinstance(req, str) and req.data else None
        self.calls.append((url, body, dict(req.header_items()) if not isinstance(req, str) else {}))
        if "tavily" in url:
            r = self.tavily.pop(0)
            if isinstance(r, int):
                raise urllib.error.HTTPError(url, r, "err", {}, None)
            return FakeResp(json.dumps({"results": r}).encode())
        if "generativelanguage" in url:
            r = self.gemini.pop(0)
            if isinstance(r, int):
                raise urllib.error.HTTPError(url, r, "err", {}, None)
            return sse(*r)
        if url.endswith("/api/ps"):
            return FakeResp(json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode())
        if url.endswith("/api/generate"):
            return FakeResp(b'{"done":true}')
        r = self.ollama.pop(0)
        if isinstance(r, int):
            raise urllib.error.HTTPError(url, r, "err", {}, None)
        return chat_stream(*r)

    def urls(self, part):
        return [c for c in self.calls if part in c[0]]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = config.load("/nonexistent.toml")
        self.cfg["cmd_dir"] = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def hybrid(self, net, tavily_key=KEY, gemini_key="g"):
        c = self.cfg
        claude = FakeClaude()
        lb = local.LocalBrain(c, urlopen=net)
        gem = local.GeminiBrain(c, urlopen=net, now=lambda: NOW, env={"GEMINI_API_KEY": gemini_key} if gemini_key else {})
        sb = search.SearchBrain(c, gem, urlopen=net, env={"TAVILY_API_KEY": tavily_key} if tavily_key else {},
                                now=lambda: NOW)
        return local.HybridBrain(c, claude, lb, gem, search=sb), claude


class RouteTest(Base):
    def test_tavily_then_gemini_summary_with_real_names(self):
        net = FakeNet(tavily=[RAMEN], gemini=[["近くの家系なら、横浜駅の吉村家と、新杉田駅の杉田家があります。"]])
        h, claude = self.hybrid(net)
        said = []
        res = h.ask("近くの家系ラーメン教えて", said.append)
        self.assertIn("吉村家", res.answer)
        self.assertEqual(claude.calls, [])
        url, body, headers = net.urls("tavily")[0]
        self.assertEqual(body["query"], "横浜 家系ラーメン")  # 「近く」は場所の名前に
        self.assertEqual(body["search_depth"], "basic")      # 1クレジット
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        # Gemini には検索結果を渡して要約させる。Google 検索は使わない（枠を温存）
        g = net.urls("generativelanguage")[0][1]
        self.assertNotIn("tools", g)
        self.assertIn("吉村家", g["systemInstruction"]["parts"][0]["text"])
        self.assertIn("3文まで", g["systemInstruction"]["parts"][0]["text"])
        self.assertTrue(said[-1].startswith("@画面 sources"))  # 調べたページを画面に
        log = res.search
        self.assertEqual(log["route"], "tavily+gemini")
        self.assertEqual(log["results"][0]["url"], "https://example.com/yoshimuraya")
        self.assertEqual(res.by, "gemini")

    def test_gemini_stopped_goes_to_claude_without_spending_search(self):
        self.cfg["gemini"]["enabled"] = False
        net = FakeNet()
        h, claude = self.hybrid(net)
        res = h.ask("近くの家系ラーメン教えて", lambda t: None)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(net.urls("tavily"), [])   # 要約できないので、検索 API の枠も使わない
        self.assertEqual([u for u, _, _ in net.calls if u.endswith("/api/chat")], [])  # ローカルでは要約しない
        self.assertEqual((res.search["route"], res.search["error"]), ("claude", "gemini_unavailable"))

    def test_gemini_quota_goes_to_claude(self):
        net = FakeNet(tavily=[RAMEN], gemini=[429])
        h, claude = self.hybrid(net)
        res = h.ask("近くの家系ラーメン教えて", lambda t: None)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(net.urls("generativelanguage")), 1)  # Gemini の検索はもう試さない
        self.assertEqual((res.search["route"], res.search["error"]), ("claude", "summary_failed"))
        # 次からは、枠が戻るまで Tavily も呼ばない
        h.ask("近くのカフェを調べて", lambda t: None)
        self.assertEqual(len(net.urls("tavily")), 1)

    def test_summary_error_goes_to_claude(self):
        net = FakeNet(tavily=[RAMEN], gemini=[500])
        h, claude = self.hybrid(net)
        res = h.ask("近くの家系ラーメン教えて", lambda t: None)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual([u for u, _, _ in net.calls if u.endswith("/api/chat")], [])
        self.assertEqual(res.search["route"], "claude")

    def test_handoff_goes_straight_to_claude(self):
        net = FakeNet(tavily=[RAMEN], gemini=[["@クロード"]])
        h, claude = self.hybrid(net)
        res = h.ask("インターネットでその店を検索して、予約できるか教えて", lambda t: None)
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(len(net.urls("generativelanguage")), 1)  # Gemini の検索も使わない
        self.assertEqual((res.search["route"], res.search["error"]), ("claude", "handoff"))

    def test_no_key_uses_old_gemini_search(self):
        net = FakeNet(gemini=[["吉村家が有名です。"]])
        h, _ = self.hybrid(net, tavily_key="")
        res = h.ask("近くの家系ラーメン教えて", lambda t: None)
        self.assertEqual(net.urls("tavily"), [])
        self.assertEqual(net.urls("generativelanguage")[0][1]["tools"], [{"google_search": {}}])
        self.assertEqual(res.search["route"], "gemini_search")
        self.assertEqual(res.search["error"], "search_unavailable")

    def test_quota_exhausted_stops_for_the_month(self):
        net = FakeNet(tavily=[432], gemini=[["一つ目。"], ["二つ目。"]])
        h, _ = self.hybrid(net)
        h.ask("近くの家系ラーメン教えて", lambda t: None)
        h.ask("近くのカフェを調べて", lambda t: None)
        self.assertEqual(len(net.urls("tavily")), 1)  # 2回目は Tavily に行かない

    def test_no_results_falls_back(self):
        net = FakeNet(tavily=[[]], gemini=[["見つかりました。"]])
        h, _ = self.hybrid(net)
        res = h.ask("近くの家系ラーメン教えて", lambda t: None)
        self.assertEqual(res.search["error"], "no_results")
        self.assertEqual(res.search["route"], "gemini_search")

    def test_deep_search_keeps_gemini_search(self):
        net = FakeNet(gemini=[["比べるとこうです。"]])
        h, _ = self.hybrid(net)
        res = h.ask("吉村家と杉田家を比べて調べて", lambda t: None)
        self.assertEqual(net.urls("tavily"), [])
        self.assertEqual(net.urls("generativelanguage")[0][1]["tools"], [{"google_search": {}}])
        self.assertEqual(res.search, {"route": "gemini_search", "deep": True})

    def test_private_questions_are_never_searched(self):
        net = FakeNet()
        h, claude = self.hybrid(net)
        h.ask("明日の予定の店を調べて", lambda t: None)
        h.ask("家計の残高を調べて", lambda t: None)
        self.assertEqual(net.urls("tavily"), [])
        self.assertEqual(len(claude.calls), 2)

    def test_country_retry_and_news_topic(self):
        net = FakeNet(tavily=[400, RAMEN], gemini=[["今日のニュースです。"]])
        h, _ = self.hybrid(net)
        h.ask("今日のニュースを教えて", lambda t: None)
        first, second = net.urls("tavily")
        self.assertEqual(first[1]["country"], "japan")
        self.assertNotIn("country", second[1])
        self.assertEqual(second[1]["topic"], "news")


class QueryTest(unittest.TestCase):
    def test_near_and_follow_up(self):
        self.assertEqual(search.build_query("近くの家系ラーメン教えて", "上星川駅"), "上星川駅 家系ラーメン")
        self.assertEqual(search.build_query("上星川駅近くのラーメンを調べて", "横浜"), "上星川駅近くのラーメン")
        self.assertEqual(search.build_query("営業時間を調べて教えてほしい", "", "上星川駅 ラーメン"),
                         "上星川駅 ラーメン 営業時間")

    def test_near_from_config(self):
        cfg = config.load("/nonexistent.toml")
        sb = search.SearchBrain(cfg, None, env={})
        self.assertEqual(sb.near(), "横浜")  # 空なら [weather] place
        cfg["search"]["near"] = "上星川駅"
        self.assertEqual(sb.near(), "上星川駅")

    def test_key_from_config_or_env(self):
        cfg = config.load("/nonexistent.toml")
        self.assertFalse(search.SearchBrain(cfg, None, env={}).available())
        self.assertTrue(search.SearchBrain(cfg, None, env={"TAVILY_API_KEY": "k"}).available())
        cfg["search"]["api_key"] = "k2"
        self.assertEqual(search.SearchBrain(cfg, None, env={}).key, "k2")


class LogTest(Base):
    def test_route_and_results_logged_without_key(self):
        net = FakeNet(tavily=[RAMEN], gemini=[["横浜駅の吉村家があります。"]])
        h, _ = self.hybrid(net)
        logs = []

        class Speaker:
            def say(self, t): pass
            def stop(self): pass
            def wait(self, timeout=None): return True

        a = Assistant(self.cfg, h, Speaker(), StateHub(), log=lambda c, q, ans, extra=None: logs.append(extra))
        a.handle_text("近くの家系ラーメン教えて")
        p = cmdlog.write_qa(self.cfg["cmd_dir"], "q", "a", extra=logs[0])
        text = p.read_text(encoding="utf-8")
        data = json.loads(text)["search"]
        self.assertEqual(data["route"], "tavily+gemini")
        self.assertEqual(data["query"], "横浜 家系ラーメン")
        self.assertEqual([r["title"] for r in data["results"]], [r["title"] for r in RAMEN])
        self.assertNotIn(KEY, text)
        self.assertTrue(Path(p).parent.name == "ray")


if __name__ == "__main__":
    unittest.main()
