"""Codex（コードの係・Claude が上限のときの受け皿）への振り分け"""

import io
import unittest

from ray import brain, codex, local
from tests.test_local import FakeClaude, FakeOllama, cfg, gemini

LIMIT_MSG = "5-hour limit reached ∙ resets 3am"


class FakeCodex:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def ask(self, text, on_text, history=None):
        self.calls.append({"text": text, "history": history})
        if self.fail:
            return brain.Result(answer="", is_error=True, by="codex")
        on_text("コーデックスの答えです。")
        return brain.Result(answer="コーデックスの答えです。", spoken=["コーデックスの答えです。"], by="codex",
                            usage={"wall_ms": 1})

    def cancel(self):
        pass


class LimitedClaude(FakeClaude):
    """5時間の上限の返事（英語のまま）を返す Claude"""

    def ask(self, text, on_text, session_id=None, confirmed=False):
        self.calls.append({"text": text, "session_id": session_id, "confirmed": confirmed})
        on_text(LIMIT_MSG)
        return brain.Result(answer=LIMIT_MSG, is_error=True, spoken=[LIMIT_MSG], session_id="s")


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(claude=None, cx=None, enabled=True):
    c = cfg()
    c["codex"]["enabled"] = enabled
    claude = claude or FakeClaude()
    cx = cx or FakeCodex()
    clock = Clock()
    h = local.HybridBrain(c, claude, local.LocalBrain(c, urlopen=FakeOllama()), gemini(key=""), codex=cx, clock=clock)
    return h, claude, cx, clock


class RouteTest(unittest.TestCase):
    def test_code_goes_to_codex_first(self):
        h, claude, cx, _ = make()
        said = []
        for q in ["このパイソンのコードをレビューして", "関数のバグを直して", "ソースコードを編集してテストを足して"]:
            res = h.ask(q, said.append)
            self.assertEqual(res.by, "codex", q)
        self.assertEqual(claude.calls, [])
        self.assertEqual(len(cx.calls), 3)
        self.assertIn(local.CONNECTING_CODEX, said)

    def test_private_stays_with_claude(self):
        h, claude, cx, _ = make()
        for q in ["今月の家計を見せて", "家計を集計するコードを書いて", "仕事のクライアントのスクリプトを直して", "予定を追加して"]:
            res = h.ask(q, lambda t: None)
            self.assertEqual(res.by, "claude", q)
        self.assertEqual(cx.calls, [])

    def test_codex_failure_falls_back_to_claude(self):
        h, claude, cx, _ = make(cx=FakeCodex(fail=True))
        res = h.ask("このコードのバグを探して", lambda t: None)
        self.assertEqual(res.by, "claude")
        self.assertEqual(len(claude.calls), 1)

    def test_off_by_default(self):
        h, claude, cx, _ = make(enabled=False)
        self.assertEqual(h.ask("関数のバグを直して", lambda t: None).by, "claude")
        self.assertEqual(cx.calls, [])

    def test_reply_to_codex_continues_with_codex(self):
        h, claude, cx, _ = make()
        cx.ask = lambda text, on_text, history=None: (cx.calls.append(text) or brain.Result(
            answer="テストも足しましょうか？", spoken=["テストも足しましょうか？"], by="codex"))
        first = h.ask("関数のバグを直して", lambda t: None)
        self.assertEqual(h.ask("はい", lambda t: None, session_id=first.session_id).by, "codex")
        self.assertEqual(claude.calls, [])

    def test_codex_does_not_get_claude_answers(self):
        h, claude, cx, _ = make()
        first = h.ask("今月の家計を見せて", lambda t: None)  # Claude（お金の話）
        h.ask("関数のバグを直して", lambda t: None, session_id=first.session_id)
        self.assertEqual(cx.calls[0]["history"], [])


class LimitTest(unittest.TestCase):
    def test_limit_switches_to_codex(self):
        h, claude, cx, clock = make(claude=LimitedClaude())
        said = []
        res = h.ask("ギットのブランチを作りたい。手順を見せて", said.append)  # Claude 行きの作業の話
        self.assertEqual(res.by, "codex")
        self.assertEqual(len(claude.calls), 1)
        self.assertEqual(said[-1], "コーデックスの答えです。")
        # しばらくは Claude を起こさずに Codex へ
        clock.t += 60
        self.assertEqual(h.ask("ファイルの一覧を見せて", lambda t: None, session_id=res.session_id).by, "codex")
        self.assertEqual(len(claude.calls), 1)
        # 休みが明けたら Claude に戻る
        clock.t += 31 * 60
        h.ask("ファイルの一覧を見せて", lambda t: None)
        self.assertEqual(len(claude.calls), 2)

    def test_limit_private_is_not_sent_to_codex(self):
        h, claude, cx, _ = make(claude=LimitedClaude())
        said = []
        res = h.ask("今月の家計を見せて", said.append)
        self.assertEqual(cx.calls, [])
        self.assertEqual(res.answer, local.CLAUDE_LIMIT)
        self.assertIn(local.CLAUDE_LIMIT, said)

    def test_is_limit(self):
        for msg in [LIMIT_MSG, "You've hit your limit · resets 3pm", "Claude AI usage limit reached|1700000000"]:
            self.assertTrue(codex.is_limit(brain.Result(answer=msg, is_error=True)), msg)
        self.assertFalse(codex.is_limit(brain.Result(answer="上限の話をしましょう。", is_error=False)))


class FakeProc:
    def __init__(self, out="", err="", code=0):
        self.out, self.err, self.returncode = out, err, code
        self.stdin_text = None

    def communicate(self, text):
        self.stdin_text = text
        return self.out, self.err

    def poll(self):
        return 0

    def kill(self):
        pass


class CodexBrainTest(unittest.TestCase):
    def run_with(self, proc):
        c = cfg()
        seen = {}

        def popen(cmd, **kw):
            seen["cmd"], seen["kw"] = cmd, kw
            return proc
        said = []
        res = codex.CodexBrain(c, popen=popen).ask("関数を直して", said.append, history=[("前の質問", "前の答え")])
        return res, said, seen

    def test_answer_from_stdout_with_voice_rules(self):
        proc = FakeProc(out="関数を直しました。\n", err="model: x\ntokens used\n2,795\n")
        res, said, seen = self.run_with(proc)
        self.assertEqual((res.answer, res.by, res.is_error), ("関数を直しました。", "codex", False))
        self.assertEqual(said, ["関数を直しました。"])
        self.assertEqual(res.usage["tokens"], 2795)
        self.assertEqual(seen["cmd"][:2], ["codex", "exec"])
        self.assertIn("--ignore-user-config", seen["cmd"])
        self.assertEqual(seen["cmd"][-1], "-")
        self.assertIn("3文まで", proc.stdin_text)
        self.assertIn("日本語だけ", proc.stdin_text)
        self.assertIn("前の答え", proc.stdin_text)
        self.assertNotIn("ANTHROPIC_API_KEY", seen["kw"]["env"])

    def test_error(self):
        res, said, _ = self.run_with(FakeProc(out="", err="ERROR: stream disconnected", code=1))
        self.assertTrue(res.is_error)
        self.assertEqual(said, [])


class LogTest(unittest.TestCase):
    def test_route_logged_as_codex(self):
        import tempfile
        from ray.app import Assistant
        from ray.ui import StateHub
        from tests.test_ray import FakeSpeaker
        h, claude, cx, _ = make()
        logged = []
        with tempfile.TemporaryDirectory() as d:
            h.cfg["cmd_dir"] = d
            a = Assistant(h.cfg, h, FakeSpeaker(), StateHub(), log=lambda c, q, ans, **kw: logged.append(kw["extra"]))
            a.handle_text("このコードをレビューして")
        self.assertEqual(logged[0]["brain"], "codex")
        self.assertEqual(logged[0]["usage"]["why"], "code")


if __name__ == "__main__":
    unittest.main()


class ScreenTest(unittest.TestCase):
    def test_stream_keeps_scene_line_whole(self):
        got = []
        out = local._Sentences(got.append)
        text = '@画面 steps {"title": "理由。", "steps": ["一つ目。", "二つ目"]}\n理由は2つです。まず一つ目です。'
        for i in range(0, len(text), 7):  # 細かく分かれて届いても、指示の行の中の「。」で切らない
            self.assertEqual(out.feed(text[i:i + 7]), "go")
        out.finish()
        self.assertEqual(got[0], '@画面 steps {"title": "理由。", "steps": ["一つ目。", "二つ目"]}\n理由は2つです。')
        self.assertEqual(got[1], "まず一つ目です。")
        self.assertEqual(out.answer, "理由は2つです。 まず一つ目です。")  # 覚えておく答えに指示は入れない
        from ray import scenes
        self.assertEqual(scenes.parse(got[0])[0].kind, "steps")

    def test_scene_without_speech_is_still_shown(self):
        got = []
        out = local._Sentences(got.append)
        out.feed('@画面 table {"title": "比較"}')
        out.finish()
        self.assertEqual(got, ['@画面 table {"title": "比較"}'])

    def test_handoff_still_works(self):
        out = local._Sentences(lambda t: None)
        self.assertEqual(out.feed("@クロード"), "handoff")

    def test_source_of_web_is_not_code(self):
        for q in ["いくつかのウェブサイトをソースに教えて", "映画のレビューを教えて", "テストの勉強法は？", "エラーって英語で何？"]:
            self.assertFalse(local.is_code(q), q)
        for q in ["このコードを直して", "関数のバグを探して", "パイソンで書いて", "単体テストを足して"]:
            self.assertTrue(local.is_code(q), q)

    def test_prompts_ask_for_screens(self):
        self.assertIn("@画面", brain.SCREEN_LITE)
        proc = FakeProc(out="はい。")
        codex.CodexBrain(cfg(), popen=lambda cmd, **kw: proc).ask("関数を直して", lambda t: None)
        self.assertIn(brain.SCREEN_LITE, proc.stdin_text)


class ShortSceneTest(unittest.TestCase):
    def test_at_kind_without_gamen_is_a_scene(self):
        got = []
        out = local._Sentences(got.append)
        text = '@diagram {"title": "流れ", "code": "flowchart LR\\nA --> B"}\n大量の文章を学びます。'
        for i in range(0, len(text), 3):
            self.assertEqual(out.feed(text[i:i + 3]), "go")
        out.finish()
        self.assertTrue(got[0].startswith("@画面 diagram {"))
        self.assertEqual(out.answer, "大量の文章を学びます。")

    def test_handoff_still_detected_in_pieces(self):
        out = local._Sentences(lambda t: None)
        self.assertEqual(out.feed("@ク"), "go")
        self.assertEqual(out.feed("ロード"), "handoff")
