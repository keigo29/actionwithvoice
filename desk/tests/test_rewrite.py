"""Claude に回す前の、ローカル LLM によるプロンプト変換"""

import io
import json
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path

from ray import brain, cmdlog, config, local, rewrite
from ray.app import Assistant
from ray.ui import StateHub

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=rewrite.JST)
LONG = "えーっとさ、あのー、来週の水曜の19時からの飲み会なんだけど、あれって誰と行くんだっけ、たしかメモに書いたと思うんだけど、ちょっと見てくれる？"
SHORT = "今日の予定は？"


class FakeResp(io.BytesIO):
    pass


class FakeOllama:
    """urlopen の代わり。/api/ps と、変換（/api/chat・流さない）に答える"""

    def __init__(self, outputs=(), loaded=True, down=False, slow=0.0):
        self.outputs = list(outputs)
        self.loaded = loaded
        self.down = down
        self.slow = slow
        self.bodies = []

    def __call__(self, req, timeout=None):
        if self.down:
            raise OSError("connection refused")
        url = req if isinstance(req, str) else req.full_url
        if url.endswith("/api/ps"):
            models = [{"name": "qwen2.5:7b"}] if self.loaded else []
            return FakeResp(json.dumps({"models": models}).encode())
        body = json.loads(req.data)
        self.bodies.append(body)
        if url.endswith("/api/generate"):
            return FakeResp(b'{"done":true}')
        if self.slow:
            threading.Event().wait(self.slow)
        out = self.outputs.pop(0)
        content = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        return FakeResp(json.dumps({"message": {"content": content}, "done": True}).encode())


class FakeClaude:
    def __init__(self):
        self.calls = []

    def ask(self, text, on_text, session_id=None, confirmed=False):
        self.calls.append({"text": text, "session_id": session_id, "confirmed": confirmed})
        on_text("クロードの答えです。")
        return brain.Result(answer="クロードの答えです。", session_id=f"claude-{len(self.calls)}",
                            spoken=["クロードの答えです。"])

    def cancel(self):
        pass


GOOD = {"依頼": "来週水曜19時からの飲み会に誰と行くか、メモから調べて答える", "要約": "",
        "参照": ["2026/11/19/デイリー.md", "Notes/ない.md", "/etc/passwd"]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "Notes").mkdir()
        (root / "Notes" / "音声連携.md").write_text("# 音声連携\n", encoding="utf-8")
        (root / "Inbox.md").write_text("# Inbox\n", encoding="utf-8")
        self.cfg = config.load("/nonexistent.toml")
        self.cfg["cmd_dir"] = str(root)
        self.cfg["gemini"]["enabled"] = False

    def tearDown(self):
        self.tmp.cleanup()

    def hybrid(self, ollama):
        claude = FakeClaude()
        lb = local.LocalBrain(self.cfg, urlopen=ollama)
        rw = rewrite.Rewriter(self.cfg, urlopen=ollama, now=lambda: NOW)
        gem = local.GeminiBrain(self.cfg, urlopen=ollama, env={})
        return local.HybridBrain(self.cfg, claude, lb, gem, rewriter=rw), claude


class WhenTest(Base):
    def test_short_question_is_not_rewritten(self):
        o = FakeOllama()
        h, claude = self.hybrid(o)
        res = h.ask(SHORT, lambda t: None)
        self.assertEqual(claude.calls[0]["text"], SHORT)
        self.assertEqual(o.bodies, [])  # 変換は走らない
        self.assertEqual(res.rewrite["applied"], False)
        self.assertEqual(res.rewrite["reason"], "short")

    def test_thresholds_can_be_changed(self):
        self.cfg["rewrite"] = {"min_chars": 3}
        r = rewrite.Rewriter(self.cfg)
        self.assertEqual(r.should(SHORT, []), "long_utterance")
        self.cfg["rewrite"] = {"enabled": False}
        self.assertEqual(r.should(LONG, []), "")

    def test_long_history_by_turns_or_chars(self):
        r = rewrite.Rewriter(self.cfg)
        self.assertEqual(r.should(SHORT, [("q", "a")] * rewrite.MIN_HISTORY_TURNS), "long_history")
        self.assertEqual(r.should(SHORT, [("q", "あ" * rewrite.MIN_HISTORY_CHARS)]), "long_history")
        self.assertEqual(r.should(LONG, [("q", "a")] * 9), "long_utterance+long_history")

    def test_confirmed_is_not_rewritten(self):
        o = FakeOllama()
        h, claude = self.hybrid(o)
        h.ask(LONG, lambda t: None, confirmed=True)
        self.assertEqual(o.bodies, [])
        self.assertEqual(claude.calls[0]["text"], LONG)

    def test_local_not_loaded_sends_original(self):
        o = FakeOllama(loaded=False)
        h, claude = self.hybrid(o)
        res = h.ask(LONG, lambda t: None)
        self.assertEqual(claude.calls[0]["text"], LONG)
        self.assertEqual(res.rewrite["error"], "local_not_ready")
        self.assertFalse([b for b in o.bodies if "messages" in b])


class ConvertTest(Base):
    def test_long_utterance_is_compressed_with_refs(self):
        o = FakeOllama(outputs=[GOOD])
        h, claude = self.hybrid(o)
        res = h.ask(LONG, lambda t: None)
        sent = claude.calls[0]["text"]
        self.assertIn("依頼：来週水曜19時からの飲み会に誰と行くか", sent)
        self.assertIn("先に見るファイル：2026/11/19/デイリー.md", sent)
        self.assertNotIn("Notes/ない.md", sent)   # 一覧にないものは落とす
        self.assertNotIn("/etc/passwd", sent)
        self.assertNotIn("えーっと", sent)
        # 変換の指示：JSON で、ローカル（Ollama）に、日本語と固有情報の決まりつき
        body = o.bodies[0]
        self.assertEqual(body["format"], "json")
        self.assertFalse(body["stream"])
        system = body["messages"][0]["content"]
        self.assertIn("一つも落とさず", system)
        self.assertIn("日本語だけ", system)
        self.assertIn("Notes/音声連携.md", system)  # 参照先の一覧
        # ログ：変換の有無と前後の字数
        log = res.rewrite
        self.assertTrue(log["applied"])
        self.assertEqual(log["reason"], "long_utterance")
        self.assertEqual(log["before_chars"], len(LONG))
        self.assertEqual(log["after_chars"], len(sent))
        self.assertFalse(log["fresh_session"])

    def test_long_history_is_summarized_and_starts_fresh_session(self):
        out = {"依頼": "その飲み会の店の場所を答える", "要約": "来週水曜19時の飲み会は佐藤さんと行く", "参照": ["Inbox.md"]}
        o = FakeOllama(outputs=[out])
        h, claude = self.hybrid(o)
        sid = None
        for i in range(rewrite.MIN_HISTORY_TURNS):
            sid = h.ask(f"予定{i}は？", lambda t: None, session_id=sid).session_id
        self.assertEqual(claude.calls[-1]["session_id"], f"claude-{rewrite.MIN_HISTORY_TURNS - 1}")
        res = h.ask("それでお店はどこ？", lambda t: None, session_id=sid)
        call = claude.calls[-1]
        self.assertIsNone(call["session_id"])  # --resume せず、要約を渡して新しく始める
        self.assertIn("これまでの話：来週水曜19時の飲み会は佐藤さんと行く", call["text"])
        self.assertIn("先に見るファイル：Inbox.md", call["text"])
        self.assertIn("持ち主：予定0は？", o.bodies[0]["messages"][1]["content"])  # 要約にはやり取りを渡す
        self.assertTrue(res.rewrite["fresh_session"])
        self.assertEqual(res.rewrite["history_turns"], rewrite.MIN_HISTORY_TURNS)
        # 次は、その新しいセッションを続ける
        self.assertEqual(h.claude_session, f"claude-{len(claude.calls)}")

    def test_cancel_during_rewrite_stops(self):
        o = FakeOllama(outputs=[GOOD], slow=0.5)
        h, claude = self.hybrid(o)
        threading.Timer(0.1, h.cancel).start()
        res = h.ask(LONG, lambda t: None)
        self.assertTrue(res.is_error)
        self.assertEqual(claude.calls, [])


class FallbackTest(Base):
    """変換に失敗しても会話は止めない（原文のまま Claude へ）"""

    def assert_original(self, ollama, error, history_turns=0):
        h, claude = self.hybrid(ollama)
        res = h.ask(LONG, lambda t: None)
        self.assertEqual(claude.calls[-1]["text"], LONG)
        self.assertEqual(res.answer, "クロードの答えです。")
        self.assertFalse(res.rewrite["applied"])
        self.assertEqual(res.rewrite["error"], error)

    def test_timeout(self):
        self.cfg["rewrite"] = {"timeout": 0.2}
        self.assert_original(FakeOllama(outputs=[GOOD], slow=1.0), "timeout")

    def test_connection_error(self):
        o = FakeOllama(outputs=[GOOD])
        h, claude = self.hybrid(o)
        o.down = True
        h.rewriter.urlopen = o
        res = h.ask(LONG, lambda t: None)  # /api/ps も落ちる → 準備できていない扱い
        self.assertEqual(claude.calls[0]["text"], LONG)
        self.assertFalse(res.rewrite["applied"])

    def test_broken_json(self):
        self.assert_original(FakeOllama(outputs=["{依頼: こわれた"]), "invalid_json")

    def test_empty(self):
        self.assert_original(FakeOllama(outputs=[{"依頼": "", "要約": "", "参照": []}]), "empty")
        self.assert_original(FakeOllama(outputs=[""]), "invalid_json")

    def test_lost_number(self):
        out = dict(GOOD, 依頼="来週水曜の飲み会に誰と行くか、メモから調べて答える")  # 19 が落ちた
        self.assert_original(FakeOllama(outputs=[out]), "lost_number")

    def test_chinese_or_english(self):
        self.assert_original(FakeOllama(outputs=[dict(GOOD, 依頼="查一下下周三19点的聚会和谁去")]), "not_japanese")
        self.assert_original(FakeOllama(outputs=[dict(GOOD, 依頼="Find who joins the party on Wed 19:00")]),
                             "not_japanese")

    def test_handoff_marker(self):
        self.assert_original(FakeOllama(outputs=[dict(GOOD, 依頼="@クロード 19時の飲み会")]), "bad_marker")

    def test_grown_too_long(self):
        out = dict(GOOD, 依頼="来週水曜19時の飲み会について" + "くわしく調べて" * 30)
        self.assert_original(FakeOllama(outputs=[out]), "too_long")

    def test_unexpected_exception(self):
        def boom(req, timeout=None):
            if isinstance(req, str):
                return FakeResp(json.dumps({"models": [{"name": "qwen2.5:7b"}]}).encode())
            raise ValueError("おかしな返事")
        self.assert_original(boom, "error: ValueError")


class CheckTest(unittest.TestCase):
    def test_keeps_names_from_utterance(self):
        q = "GitHub の Cmd リポジトリで、2026-11-20 の予定を確かめて、もし無かったら追加しておいてほしいんだけど"
        ok, why = rewrite.check({"依頼": "GitHubのCmdで2026-11-20の予定を確かめ、無ければ追加する"}, q, q, {}, False)
        self.assertEqual(why, "")
        _, why = rewrite.check({"依頼": "Cmdで2026-11-20の予定を確かめ、無ければ追加する"}, q, q, {}, False)
        self.assertEqual(why, "lost_name")

    def test_full_width_digits_count_as_same(self):
        q = "えっと、１９時からの飲み会って、誰と行くんだっけ"
        _, why = rewrite.check({"依頼": "19時からの飲み会に誰と行くか答える"}, q, q, {}, False)
        self.assertEqual(why, "")

    def test_paths_from_list_are_allowed(self):
        q = "えっと、音声連携のノートに書いた、プロンプト変換の決まりって何だったっけ"
        cands = {"Notes/音声連携.md": ""}
        _, why = rewrite.check({"依頼": "Notes/音声連携.md のプロンプト変換の決まりを答える"}, q, q + "\n".join(cands), cands, False)
        self.assertEqual(why, "")

    def test_history_needs_summary(self):
        _, why = rewrite.check({"依頼": "店の場所を答える", "要約": ""}, "店はどこ？", "", {}, True)
        self.assertEqual(why, "no_summary")


class LogTest(Base):
    def test_rewrite_is_written_to_data_ray(self):
        o = FakeOllama(outputs=[GOOD])
        h, _ = self.hybrid(o)
        logs = []

        class Speaker:
            def say(self, t): pass
            def stop(self): pass
            def wait(self, timeout=None): return True

        self.cfg["ui"]["enabled"] = False
        a = Assistant(self.cfg, h, Speaker(), StateHub(), log=lambda c, q, ans, extra=None: logs.append(extra))
        a.handle_text(LONG)
        p = cmdlog.write_qa(self.cfg["cmd_dir"], LONG, "a", extra=logs[0])
        data = json.loads(p.read_text(encoding="utf-8"))
        self.assertTrue(data["rewrite"]["applied"])
        self.assertEqual(data["rewrite"]["before_chars"], len(LONG))
        self.assertGreater(data["rewrite"]["after_chars"], 0)
        self.assertEqual(data["rewrite"]["refs"], ["2026/11/19/デイリー.md"])
        self.assertIn("Data/ray", str(p))


if __name__ == "__main__":
    unittest.main()
