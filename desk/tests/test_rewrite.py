"""Claude に回す前のプロンプト変換（抽出はコード → 整形は 7B → 検証はコード）"""

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

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=rewrite.JST)  # 木曜
LONG = "えーっとさ、あのー、来週の水曜の19時からの飲み会なんだけど、あれって誰と行くんだっけ、たしかメモに書いたと思うんだけど、ちょっと見てくれる？"
SHORT = "今日の予定は？"
GOOD = {"依頼": "来週水曜19時からの飲み会に誰と行くか、メモを見て答える", "要約": ""}


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


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = root = Path(self.tmp.name)
        for rel in ("Notes/体の記録.md", "Projects/減量.md", "Notes/音声連携.md", "Data/kakei.json",
                    "Inbox.md", "Projects/副業.md"):
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text("x\n", encoding="utf-8")
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


# ---------------------------------------------------------------- いつ変換するか

class WhenTest(Base):
    def test_short_question_is_not_rewritten(self):
        o = FakeOllama()
        h, claude = self.hybrid(o)
        res = h.ask(SHORT, lambda t: None)
        self.assertEqual(claude.calls[0]["text"], SHORT)
        self.assertEqual(o.bodies, [])  # 変換は走らない
        self.assertEqual(res.rewrite["applied"], False)
        self.assertEqual(res.rewrite["reason"], "short")
        self.assertNotIn("before", res.rewrite)

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


# ---------------------------------------------------------------- 1. 抽出（コード）

class RefRulesTest(Base):
    def refs(self, text):
        return local.ref_paths(text, self.root, NOW)

    def test_keywords_to_files(self):
        self.assertEqual(self.refs("最近の体重はどう？"), ["Notes/体の記録.md", "Projects/減量.md"])
        self.assertEqual(self.refs("今月の家計、残高はいくら？")[0], "Data/kakei.json")
        self.assertEqual(self.refs("今日の予定を教えて"), ["2026/11/19/デイリー.md"])
        self.assertEqual(self.refs("明日の予定"), ["2026/11/20/デイリー.md"])
        self.assertEqual(self.refs("あさってのやること"), ["2026/11/21/デイリー.md", "Inbox.md"])
        self.assertIn("Projects/副業.md", self.refs("副業の案件どうなってる"))

    def test_missing_files_are_skipped(self):
        # 筋トレのノートはこの Cmd に無いので添えない
        self.assertEqual(self.refs("筋トレの記録を見せて"), [])

    def test_said_dates_become_that_daily(self):
        self.assertEqual(self.refs("来週の水曜の予定"), ["2026/11/25/デイリー.md"])
        self.assertEqual(self.refs("11月30日の予定"), ["2026/11/30/デイリー.md"])
        self.assertEqual(self.refs("2026-12-01 の予定"), ["2026/12/01/デイリー.md"])
        self.assertEqual(local.said_dates("今週の月曜と金曜", NOW), ["2026-11-16", "2026-11-20"])

    def test_note_name_said_as_is(self):
        self.assertIn("Notes/音声連携.md", self.refs("音声連携のノートに書いた決まりは？"))

    def test_at_most_four(self):
        self.assertLessEqual(len(self.refs("体重と家計と副業と今日の予定と明日の予定と昨日のメモ")), local.REF_MAX)


class TidyTest(unittest.TestCase):
    def test_fillers_and_hedges(self):
        t = rewrite.tidy(LONG)
        for w in ("えーっと", "あのー", "なんだけど", "と思うんだけど"):
            self.assertNotIn(w, t)
        self.assertTrue(t.startswith("来週の水曜の19時からの飲み会"))

    def test_address_repeat_and_correction(self):
        t = rewrite.tidy("ねえレイ、今日の、今日の予定で、水曜、いや木曜の会議って何時からだっけ")
        self.assertEqual(t, "今日の予定で、木曜の会議って何時からだっけ")

    def test_keeps_numbers_and_reduplicated_words(self):
        t = rewrite.tidy("いろいろ聞きたいんだけど、まあ、3万円の支払いと19時の件")
        self.assertEqual(t, "いろいろ聞きたい、3万円の支払いと19時の件")


# ---------------------------------------------------------------- 3. 検証（コード）

class FactsTest(unittest.TestCase):
    def test_same_number_in_other_forms(self):
        self.assertEqual(rewrite.missing_facts("来週水曜19時、3万円", "来週の水曜日の十九時、30000円"), [])
        self.assertEqual(rewrite.missing_facts("１９時から", "19時から"), [])
        self.assertEqual(rewrite.missing_facts("あしたの予定", "明日の予定"), [])
        self.assertEqual(rewrite.missing_facts("2026-11-20 の", "11月20日、2026年の"), [])

    def test_detects_lost_numbers_and_dates(self):
        self.assertEqual(rewrite.missing_facts("来週水曜19時の飲み会", "水曜の飲み会"), ["数 19", "日 来週"])
        self.assertEqual(rewrite.missing_facts("11月20日と21日", "11月20日"), ["数 21"])
        self.assertEqual(rewrite.missing_facts("金曜の GitHub", "金曜"), ["名 github"])

    def test_words_like_issho_are_not_numbers(self):
        self.assertEqual(rewrite.numbers("一緒に一番"), [])
        self.assertEqual(rewrite.numbers("三万五千円と二十日"), ["35000", "20"])


# ---------------------------------------------------------------- 流れ全体

class ConvertTest(Base):
    def test_long_utterance_refs_by_rule_and_passes_check(self):
        o = FakeOllama(outputs=[GOOD])
        h, claude = self.hybrid(o)
        res = h.ask(LONG, lambda t: None)
        sent = claude.calls[0]["text"]
        self.assertIn("依頼：来週水曜19時からの飲み会に誰と行くか、メモを見て答える", sent)
        self.assertIn("先に見るファイル：2026/11/25/デイリー.md", sent)  # 来週水曜＝その日のデイリー（ルール）
        self.assertNotIn("持ち主の言葉", sent)  # 検証を通ったので原文は併記しない
        # 7B に渡すのは下ごしらえした文。形は JSON スキーマで固定し、ファイルの推定はさせない
        body = o.bodies[0]
        self.assertEqual(body["format"], rewrite.SCHEMA)
        self.assertFalse(body["stream"])
        self.assertNotIn("えーっと", body["messages"][1]["content"])
        self.assertNotIn("参照", body["messages"][0]["content"])
        self.assertIn("一つも落とさず", body["messages"][0]["content"])
        # ログ：変換の有無と前後の組
        log = res.rewrite
        self.assertTrue(log["applied"])
        self.assertEqual(log["before"], LONG)
        self.assertEqual(log["after"], sent)
        self.assertEqual((log["before_chars"], log["after_chars"]), (len(LONG), len(sent)))
        self.assertEqual(log["refs"], ["2026/11/25/デイリー.md"])
        self.assertEqual(log["missing"], [])

    def test_lost_number_is_detected_and_original_attached(self):
        o = FakeOllama(outputs=[dict(GOOD, 依頼="来週水曜の飲み会に誰と行くか、メモを見て答える")])  # 19 が落ちた
        h, claude = self.hybrid(o)
        res = h.ask(LONG, lambda t: None)
        sent = claude.calls[0]["text"]
        self.assertIn("依頼：来週水曜の飲み会に", sent)
        self.assertIn(f"持ち主の言葉（まとめで抜けた所があるので、こちらを正とする）：{LONG}", sent)
        self.assertTrue(res.rewrite["with_original"])
        self.assertEqual(res.rewrite["missing"], ["数 19"])

    def test_lost_date_is_detected(self):
        o = FakeOllama(outputs=[dict(GOOD, 依頼="水曜19時からの飲み会に誰と行くか、メモを見て答える")])  # 来週が落ちた
        h, claude = self.hybrid(o)
        res = h.ask(LONG, lambda t: None)
        self.assertEqual(res.rewrite["missing"], ["日 来週"])
        self.assertIn(LONG, claude.calls[0]["text"])

    def test_corrected_word_is_not_counted_as_lost(self):
        q = "ねえレイ、今日の、今日の予定で、水曜、いや木曜の会議って何時からだっけ、カレンダーに入れたと思うんだけど、えーっと、どこだっけ"
        o = FakeOllama(outputs=[{"依頼": "今日の予定で、木曜の会議が何時からか答える", "要約": ""}])
        h, claude = self.hybrid(o)
        res = h.ask(q, lambda t: None)
        self.assertEqual(res.rewrite["missing"], [])
        self.assertEqual(res.rewrite["tidied"], "今日の予定で、木曜の会議って何時からだっけ、カレンダーに入れた、どこだっけ")
        self.assertEqual(res.rewrite["refs"], ["2026/11/19/デイリー.md"])

    def test_long_history_is_summarized_and_starts_fresh_session(self):
        out = {"依頼": "その飲み会の店の場所を答える", "要約": "来週水曜19時の飲み会は佐藤さんと行く"}
        o = FakeOllama(outputs=[out])
        h, claude = self.hybrid(o)
        sid = None
        for i in range(rewrite.MIN_HISTORY_TURNS - 1):
            sid = h.ask(f"予定{i}は？", lambda t: None, session_id=sid).session_id
        sid = h.ask("来週水曜19時の飲み会は誰と？", lambda t: None, session_id=sid).session_id
        res = h.ask("それでお店はどこ？", lambda t: None, session_id=sid)
        call = claude.calls[-1]
        self.assertIsNone(call["session_id"])  # --resume せず、要約を渡して新しく始める
        self.assertIn("これまでの話：来週水曜19時の飲み会は佐藤さんと行く", call["text"])
        # 今の発話に参照の言葉が無ければ、直前の質問からルールで決める
        self.assertIn("先に見るファイル：2026/11/25/デイリー.md", call["text"])
        self.assertIn("持ち主：予定0は？", o.bodies[0]["messages"][1]["content"])
        self.assertTrue(res.rewrite["fresh_session"])
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

    def assert_original(self, ollama, error):
        h, claude = self.hybrid(ollama)
        res = h.ask(LONG, lambda t: None)
        self.assertEqual(claude.calls[-1]["text"], LONG)
        self.assertEqual(res.answer, "クロードの答えです。")
        self.assertFalse(res.rewrite["applied"])
        self.assertEqual(res.rewrite["error"], error)
        self.assertEqual(res.rewrite["before"], LONG)

    def test_timeout(self):
        self.cfg["rewrite"] = {"timeout": 0.2}
        self.assert_original(FakeOllama(outputs=[GOOD], slow=1.0), "timeout")

    def test_broken_json(self):
        self.assert_original(FakeOllama(outputs=["{依頼: こわれた"]), "invalid_json")

    def test_empty(self):
        self.assert_original(FakeOllama(outputs=[{"依頼": "", "要約": ""}]), "empty")
        self.assert_original(FakeOllama(outputs=[""]), "invalid_json")

    def test_invented_number(self):
        self.assert_original(FakeOllama(outputs=[dict(GOOD, 依頼="来週水曜20時からの飲み会に誰と行くか答える")]),
                             "invented_number")

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
        self.assertIn("Data/ray", str(p))
        data = json.loads(p.read_text(encoding="utf-8"))["rewrite"]
        self.assertTrue(data["applied"])
        self.assertEqual(data["before"], LONG)
        self.assertTrue(data["after"].startswith("（Ray が持ち主の話を短くまとめた依頼"))
        self.assertEqual(data["refs"], ["2026/11/25/デイリー.md"])


if __name__ == "__main__":
    unittest.main()
