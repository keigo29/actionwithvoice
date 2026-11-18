"""ローカル LLM の一次受けと、Claude への振り分け"""

import io
import json
import unittest
import urllib.error
from datetime import datetime

from ray import brain, config, local


def cfg():
    c = config.load("/nonexistent.toml")
    c["cmd_dir"] = "/tmp"
    return c


class FakeResp(io.BytesIO):
    pass


def chat_stream(*pieces):
    lines = [json.dumps({"message": {"content": p}, "done": False}) for p in pieces]
    lines.append(json.dumps({"message": {"content": ""}, "done": True}))
    return FakeResp(("\n".join(lines) + "\n").encode())


class FakeOllama:
    """urlopen の代わり。/api/ps と /api/chat と /api/generate に答える"""

    def __init__(self, chats=(), loaded=True, down=False):
        self.chats = list(chats)
        self.loaded = loaded
        self.down = down
        self.bodies = []
        self.paths = []

    def __call__(self, req, timeout=None):
        if self.down:
            raise OSError("connection refused")
        url = req if isinstance(req, str) else req.full_url
        path = url.split("11434")[1]
        self.paths.append(path)
        if path == "/api/ps":
            models = [{"name": "qwen2.5:7b"}] if self.loaded else []
            return FakeResp(json.dumps({"models": models}).encode())
        body = json.loads(req.data)
        self.bodies.append(body)
        if path == "/api/generate":
            return FakeResp(b'{"done":true}')
        return chat_stream(*self.chats.pop(0))


class FakeClaude:
    def __init__(self):
        self.calls = []
        self.cancelled = 0

    def ask(self, text, on_text, session_id=None, confirmed=False):
        self.calls.append({"text": text, "session_id": session_id, "confirmed": confirmed})
        on_text("クロードの答えです。")
        return brain.Result(answer="クロードの答えです。", session_id=f"claude-{len(self.calls)}",
                            spoken=["クロードの答えです。"])

    def cancel(self):
        self.cancelled += 1


def sse(*pieces):
    """文字の断片か、そのまま流す1つの出来事（dict）を並べる"""
    evs = [p if isinstance(p, dict) else {"candidates": [{"content": {"parts": [{"text": p}]}}]} for p in pieces]
    return FakeResp(b"\n".join(b"data: " + json.dumps(e).encode() + b"\n" for e in evs))


class FakeGeminiHttp:
    """Gemini API の代わり。answers の1つずつを返す（int なら、その番号の HTTP エラー）"""

    def __init__(self, answers=()):
        self.answers = list(answers)
        self.bodies = []

    def __call__(self, req, timeout=None):
        self.bodies.append(json.loads(req.data))
        a = self.answers.pop(0)
        if isinstance(a, int):
            raise urllib.error.HTTPError(req.full_url, a, "err", {}, None)
        return sse(*a)


def gemini(http=None, key="k", now=lambda: datetime(2026, 11, 19, 12, 0, tzinfo=local.JST)):
    return local.GeminiBrain(cfg(), urlopen=http or FakeGeminiHttp(), now=now, env={"GEMINI_API_KEY": key} if key else {})


def hybrid(ollama, gem=None):
    c = cfg()
    claude = FakeClaude()
    g = gem or gemini(key="")  # 鍵なし＝Gemini の段は飛ばす
    return local.HybridBrain(c, claude, local.LocalBrain(c, urlopen=ollama), g), claude


class RuleTest(unittest.TestCase):
    def test_needs_claude(self):
        # 持ち主のデータ（お金・仕事・予定やメモ）と作業は Claude
        for q in ["今日の予定は？", "インボックスに牛乳を追加して", "明日の天気", "口座の残高", "仕事の資料",
                  "Cmd で一番大きいフォルダは？", "おはよう", "git の状態", "体重を記録して"]:
            self.assertTrue(local.needs_claude(q), q)
        for q in ["こんにちは", "フランスの首都は？", "37かける12は？", "最近ちょっと疲れてる",
                  "最新のニュースを調べて", "体重の推移", "量子もつれを詳しく説明して"]:
            self.assertFalse(local.needs_claude(q), q)

    def test_search_and_health(self):
        self.assertTrue(local.needs_search("最新のニュースを調べて"))
        self.assertTrue(local.needs_search("新しいアイフォーンの値段はいくら？"))
        self.assertFalse(local.needs_search("フランスの首都は？"))
        self.assertEqual(local.health_kinds("最近の体重と筋トレどう？"), ["body", "workout"])
        self.assertEqual(local.health_kinds("フランスの首都は？"), [])


class HybridTest(unittest.TestCase):
    def test_local_answers_sentence_by_sentence(self):
        o = FakeOllama(chats=[["パリ", "です。首都", "は", "セーヌ川の近くです。"]])
        h, claude = hybrid(o)
        said = []
        res = h.ask("こんにちは", said.append)
        self.assertEqual(said, ["パリです。", "首都はセーヌ川の近くです。"])
        self.assertEqual(res.by, "local")
        self.assertEqual(claude.calls, [])
        self.assertEqual(o.bodies[0]["messages"][-1], {"role": "user", "content": "こんにちは"})
        self.assertIn("今は", o.bodies[0]["messages"][0]["content"])

    def test_handoff_marker_goes_to_claude(self):
        o = FakeOllama(chats=[["@", "クロ", "ード"]])
        h, claude = hybrid(o)
        said = []
        res = h.ask("量子コンピューターの仕組み", said.append)
        self.assertEqual(said, ["クロードの答えです。"])
        self.assertEqual(res.by, "claude")
        self.assertEqual(len(claude.calls), 1)

    def test_marker_after_answer_is_dropped(self):
        o = FakeOllama(chats=[["元気です。", "@クロード"]])
        h, claude = hybrid(o)
        said = []
        res = h.ask("元気？", said.append)
        self.assertEqual(said, ["元気です。"])
        self.assertEqual(res.by, "local")
        self.assertEqual(claude.calls, [])

    def test_explanations_skip_local(self):
        # 説明は画面つきで答えるので、ローカルには聞かない（7B は「@クロード」を守らないことがある）
        o = FakeOllama(chats=[["秒速30万キロです。"]])
        h, claude = hybrid(o)
        res = h.ask("光の速さってどれくらい？", lambda t: None)
        self.assertEqual(res.by, "claude")
        self.assertNotIn("/api/chat", o.paths)
        self.assertTrue(local.is_chat("こんにちは"))
        self.assertTrue(local.is_chat("今何時？"))
        self.assertFalse(local.is_chat("こんにちは、量子コンピューターの仕組みを教えて"))

    def test_rule_skips_local(self):
        o = FakeOllama()
        h, claude = hybrid(o)
        h.ask("今日の予定は？", lambda t: None)
        self.assertEqual(len(claude.calls), 1)
        self.assertNotIn("/api/chat", o.paths)

    def test_confirmed_always_claude(self):
        o = FakeOllama()
        h, claude = hybrid(o)
        h.ask("はい", lambda t: None, confirmed=True)
        self.assertTrue(claude.calls[0]["confirmed"])
        self.assertNotIn("/api/chat", o.paths)

    def test_down_or_not_loaded_falls_back(self):
        h, claude = hybrid(FakeOllama(down=True))
        self.assertEqual(h.ask("こんにちは", lambda t: None).by, "claude")
        o = FakeOllama(loaded=False)
        h, claude = hybrid(o)
        self.assertEqual(h.ask("こんにちは", lambda t: None).by, "claude")
        self.assertNotIn("/api/chat", o.paths)

    def test_conversation_shares_history_and_claude_session(self):
        o = FakeOllama(chats=[["こんにちは。"], ["どういたしまして。"]])
        h, claude = hybrid(o)
        r1 = h.ask("こんにちは", lambda t: None)
        # Claude には、ローカルで答えたやり取りを添えて渡す
        r2 = h.ask("それってどういうこと？", lambda t: None, session_id=r1.session_id)
        self.assertIn("持ち主：こんにちは\nレイ：こんにちは。", claude.calls[0]["text"])
        self.assertIsNone(claude.calls[0]["session_id"])
        self.assertEqual(r1.session_id, r2.session_id)
        # ローカルには、Claude の答えも含めた流れを渡す
        h.ask("ありがとう", lambda t: None, session_id=r2.session_id)
        msgs = o.bodies[-1]["messages"]
        self.assertIn({"role": "assistant", "content": "クロードの答えです。"}, msgs)
        # 次に Claude に回すときは、同じ Claude のセッションを続け、もう知っている分は添えない
        h.ask("予定は？", lambda t: None, session_id=r2.session_id)
        self.assertEqual(claude.calls[1]["session_id"], "claude-1")
        self.assertIn("持ち主：ありがとう", claude.calls[1]["text"])
        self.assertNotIn("持ち主：こんにちは\n", claude.calls[1]["text"])

    def test_new_conversation_resets(self):
        o = FakeOllama(chats=[["こんにちは。"], ["はい。"]])
        h, claude = hybrid(o)
        r1 = h.ask("こんにちは", lambda t: None)
        r2 = h.ask("元気？", lambda t: None, session_id=None)
        self.assertNotEqual(r1.session_id, r2.session_id)
        self.assertEqual(len(o.bodies[-1]["messages"]), 2)  # 指示と今の質問だけ

    def test_keep_loaded_while_running_and_unload_on_close(self):
        o = FakeOllama()
        h, _ = hybrid(o)
        h.warm(block=True)
        self.assertEqual(o.bodies[-1]["keep_alive"], -1)
        h.close()
        self.assertEqual(o.bodies[-1], {"model": "qwen2.5:7b", "keep_alive": 0})

    def test_health_question_reads_cmd_and_stays_local(self):
        o = FakeOllama(chats=[["体重は少し減っています。"]])
        g = gemini(FakeGeminiHttp())
        h, claude = hybrid(o, g)
        orig = local.health_text
        local.health_text = lambda c, kinds: f"記録：{kinds}"
        try:
            res = h.ask("最近の体重どう？", lambda t: None)
        finally:
            local.health_text = orig
        self.assertEqual(res.by, "local")
        self.assertIn("記録：['body']", o.bodies[0]["messages"][0]["content"])
        self.assertEqual(g.urlopen.bodies, [])  # 体調の記録は外に出さない
        self.assertEqual(claude.calls, [])

    def test_health_falls_back_to_gemini_with_records(self):
        http = FakeGeminiHttp([["体重は横ばいです。"]])
        h, claude = hybrid(FakeOllama(down=True), gemini(http))
        orig = local.health_text
        local.health_text = lambda c, kinds: "体重：11-16 70.4"
        try:
            res = h.ask("最近の体重どう？", lambda t: None)
        finally:
            local.health_text = orig
        self.assertEqual(res.by, "gemini")
        self.assertIn("体重：11-16 70.4", http.bodies[0]["systemInstruction"]["parts"][0]["text"])
        self.assertEqual(claude.calls, [])

    def test_local_handoff_goes_to_gemini_then_claude(self):
        http = FakeGeminiHttp([["ゲミニの答えです。"], ["@クロード"]])
        h, claude = hybrid(FakeOllama(chats=[["@クロード"], ["@クロード"]]), gemini(http))
        self.assertEqual(h.ask("ブラックホールって何？", lambda t: None).by, "gemini")
        r = h.ask("それはなぜ？", lambda t: None, session_id=h.conv)
        self.assertEqual(r.by, "claude")  # Gemini も無理なら Claude
        self.assertNotIn("tools", http.bodies[0])

    def test_deep_skips_local(self):
        o = FakeOllama()
        h, claude = hybrid(o, gemini(FakeGeminiHttp([["くわしい答えです。"]])))
        self.assertEqual(h.ask("量子もつれを詳しく説明して", lambda t: None).by, "gemini")
        self.assertNotIn("/api/chat", o.paths)

    def test_search_goes_to_gemini_with_google_search(self):
        http = FakeGeminiHttp([["今日のニュースは三つです。"]])
        o = FakeOllama()
        h, claude = hybrid(o, gemini(http))
        res = h.ask("最新のニュースを調べて", lambda t: None)
        self.assertEqual(res.by, "gemini")
        self.assertEqual(http.bodies[0]["tools"], [{"google_search": {}}])
        self.assertNotIn("/api/chat", o.paths)
        self.assertEqual(claude.calls, [])

    def test_search_without_gemini_goes_to_claude(self):
        h, claude = hybrid(FakeOllama())  # 鍵なし
        self.assertEqual(h.ask("最新のニュースを調べて", lambda t: None).by, "claude")

    def test_gemini_never_gets_claude_answers(self):
        http = FakeGeminiHttp([["ゲミニの答えです。"]])
        h, claude = hybrid(FakeOllama(chats=[["@クロード"]]), gemini(http))
        r = h.ask("今月の家計は？", lambda t: None)              # Claude（お金）
        h.ask("ブラックホールって何？", lambda t: None, session_id=r.session_id)
        sent = json.dumps(http.bodies[0], ensure_ascii=False)
        self.assertNotIn("家計", sent)
        self.assertNotIn("クロードの答え", sent)

    def test_cancel_stops_both(self):
        h, claude = hybrid(FakeOllama())
        h.cancel()
        self.assertEqual(claude.cancelled, 1)


class PanelTest(unittest.TestCase):
    def test_with_panel_only_on_first_sentence(self):
        got = []
        f = local.with_panel(got.append, local.panel_line("body", {"days": 30}))
        f("一つ目。")
        f("二つ目。")
        self.assertEqual(got, ['@画面 body {"days": 30}\n一つ目。', "二つ目。"])
        self.assertEqual(local.health_panel(["habits", "workout"]), local.panel_line("workout", {"days": 90}))
        self.assertEqual(local.health_panel([]), "")

    def test_gemini_search_shows_sources_after_answer(self):
        grounding = {"candidates": [{"content": {"parts": [{"text": "です。"}]}, "groundingMetadata": {"groundingChunks": [
            {"web": {"uri": "https://a.example/1", "title": "a.example"}},
            {"web": {"uri": "https://a.example/1", "title": "a.example"}},
            {"web": {"uri": "https://b.example/2", "title": "b.example"}}]}}]}
        got = []
        g = gemini(FakeGeminiHttp([["今日は雪", grounding]]))
        self.assertEqual(g.ask([], "ニュースを調べて", got.append, search=True), "今日は雪です。")
        self.assertEqual(got[0], "今日は雪です。")
        self.assertTrue(got[1].startswith("@画面 sources "))
        items = json.loads(got[1].split(" ", 2)[2])["items"]
        self.assertEqual([i["url"] for i in items], ["https://a.example/1", "https://b.example/2"])

    def test_assistant_shows_panels_from_local_and_gemini(self):
        from ray.app import Assistant
        from ray.ui import StateHub
        from tests.test_ray import FakeSpeaker

        grounding = {"candidates": [{"content": {"parts": [{"text": ""}]}, "groundingMetadata": {
            "groundingChunks": [{"web": {"uri": "https://news.example/x", "title": "news.example"}}]}}]}
        o = FakeOllama(chats=[["体重はほぼ横ばいです。", "記録を続けましょう。"]])
        h, claude = hybrid(o, gemini(FakeGeminiHttp([["大雪のおそれがあります。", grounding]])))
        orig = local.health_text
        local.health_text = lambda c, kinds: "体重：11-16 70.4"
        speaker, hub = FakeSpeaker(), StateHub()
        a = Assistant(cfg(), h, speaker, hub, log=lambda *x, **k: None)
        try:
            a.handle_text("最近の体重どう？")
        finally:
            local.health_text = orig
        self.assertEqual(hub.state["scene"]["kind"], "body")
        self.assertEqual(speaker.said, ["体重はほぼ横ばいです。", "記録を続けましょう。"])

        a.handle_text("最新のニュースを調べて")
        self.assertEqual(hub.state["scene"]["kind"], "sources")
        self.assertEqual(hub.state["scene"]["data"]["items"][0]["url"], "https://news.example/x")
        self.assertEqual(speaker.said[-1], "大雪のおそれがあります。")  # 指示の行は読み上げない
        self.assertEqual(claude.calls, [])


class GeminiTest(unittest.TestCase):
    def test_no_key_is_unavailable(self):
        self.assertFalse(gemini(key="").available())

    def test_429_stops_until_17_jst(self):
        t = {"now": datetime(2026, 11, 19, 12, 0, tzinfo=local.JST)}
        http = FakeGeminiHttp([429, ["また使えます。"]])
        g = gemini(http, now=lambda: t["now"])
        self.assertIsNone(g.ask([], "質問", lambda x: None))
        self.assertFalse(g.available())
        t["now"] = datetime(2026, 11, 19, 16, 59, tzinfo=local.JST)
        self.assertFalse(g.available())
        t["now"] = datetime(2026, 11, 19, 17, 0, tzinfo=local.JST)  # 日本時間17時に戻る
        self.assertTrue(g.available())
        self.assertEqual(g.ask([], "質問", lambda x: None), "また使えます。")

    def test_daily_limit(self):
        http = FakeGeminiHttp([["一。"], ["二。"]])
        g = gemini(http)
        g.c = dict(g.c, daily_limit=1)
        self.assertEqual(g.ask([], "質問", lambda x: None), "一。")
        self.assertIsNone(g.ask([], "質問", lambda x: None))  # 上限の手前で止める

    def test_streams_sentences_and_sends_key_in_header(self):
        seen = []

        class Http(FakeGeminiHttp):
            def __call__(self, req, timeout=None):
                seen.append(req.get_header("X-goog-api-key"))
                return super().__call__(req, timeout)

        said = []
        g = gemini(Http([["パリ", "です。セーヌ川が", "流れています。"]]), key="secret")
        self.assertEqual(g.ask([], "首都は？", said.append), "パリです。 セーヌ川が流れています。")
        self.assertEqual(said, ["パリです。", "セーヌ川が流れています。"])
        self.assertEqual(seen, ["secret"])


if __name__ == "__main__":
    unittest.main()
