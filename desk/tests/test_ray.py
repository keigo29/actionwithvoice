import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ray import brain, cmdlog, config, speech, stt
from ray.app import CONFIRMED_PROMPT, Assistant
from ray.audio import RATE, SilenceDetector, duration, rms, save_wav
from ray.ui import StateHub

JST = timezone(timedelta(hours=9))


def cfg(**over):
    c = config.load("/nonexistent.toml")
    c["cmd_dir"] = over.pop("cmd_dir", "/tmp")
    c.update(over)
    return c


class BrainTest(unittest.TestCase):
    def test_build_command(self):
        cmd = brain.build_command(cfg())
        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("stream-json", cmd)
        self.assertIn("--verbose", cmd)
        self.assertIn("Bash(du:*)", cmd)
        self.assertNotIn("Bash", cmd)  # ふだんはコマンド全部は許さない
        self.assertNotIn("--resume", cmd)
        # 質問は stdin で渡す（引数の最後にくっつけない）
        self.assertEqual(cmd[-1], "--strict-mcp-config")
        # 速さ優先の既定：Sonnet・考える量は少なめ・MCP は読み込まない
        self.assertEqual(cmd[cmd.index("--model") + 1], "sonnet")
        self.assertEqual(cmd[cmd.index("--effort") + 1], "low")

    def test_build_command_can_use_defaults_and_mcp(self):
        c = cfg()
        c["claude"].update(model="", effort="", mcp=True)
        cmd = brain.build_command(c)
        for flag in ("--model", "--effort", "--strict-mcp-config"):
            self.assertNotIn(flag, cmd)

    def test_build_command_confirmed_and_resume(self):
        cmd = brain.build_command(cfg(), session_id="abc", confirmed=True)
        self.assertIn("Bash", cmd)
        self.assertEqual(cmd[cmd.index("--resume") + 1], "abc")
        self.assertIn("Bash(git push --force:*)", cmd)

    def test_clean_env_drops_api_keys(self):
        env = brain.clean_env({"ANTHROPIC_API_KEY": "x", "ANTHROPIC_AUTH_TOKEN": "y", "PATH": "/bin"})
        self.assertEqual(env, {"PATH": "/bin"})

    def test_parse_stream(self):
        lines = [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
            "not json",
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "調べます。"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "一番大きいのは Data です。"}]}}),
            json.dumps({"type": "result", "subtype": "success", "result": "一番大きいのは Data です。", "session_id": "s1"}),
        ]
        said = []
        res = brain.parse_stream(lines, said.append)
        self.assertEqual(said, ["調べます。", "一番大きいのは Data です。"])  # 同じ文を2回言わない
        self.assertEqual(res.answer, "一番大きいのは Data です。")
        self.assertEqual(res.session_id, "s1")
        self.assertFalse(res.is_error)

    def test_parse_stream_result_only_and_error(self):
        said = []
        res = brain.parse_stream([json.dumps({"type": "result", "subtype": "error_max_turns", "result": ""})], said.append)
        self.assertTrue(res.is_error)
        res = brain.parse_stream([json.dumps({"type": "result", "subtype": "success", "result": "はい"})], said.append)
        self.assertEqual(said, ["はい"])

    def test_confirm_and_reply(self):
        self.assertTrue(brain.is_confirm("確認です。node_modules を消します。よいですか？"))
        self.assertFalse(brain.is_confirm("消しました"))
        self.assertEqual(brain.classify_reply("はい、お願い"), "yes")
        self.assertEqual(brain.classify_reply("OK"), "yes")
        self.assertEqual(brain.classify_reply("いや、やめて"), "no")
        self.assertEqual(brain.classify_reply("待って"), "no")
        self.assertEqual(brain.classify_reply("ところで天気は？"), "other")

    def test_brain_ask_with_fake_process(self):
        class FakeProc:
            def __init__(self, cmd, **kw):
                self.cmd, self.kw = cmd, kw
                self.stdin = self
                self.written = ""
                self.stdout = iter([json.dumps({"type": "result", "subtype": "success", "result": "了解です", "session_id": "z"})])
                self.stderr = None
                FakeProc.last = self

            def write(self, t):
                self.written += t

            def close(self):
                pass

            def wait(self):
                return 0

            def poll(self):
                return 0

            def kill(self):
                pass

        b = brain.Brain(cfg(cmd_dir="/work"), popen=FakeProc)
        res = b.ask("Inbox に牛乳", lambda t: None)
        self.assertEqual(res.answer, "了解です")
        self.assertEqual(FakeProc.last.written, "Inbox に牛乳")
        self.assertEqual(FakeProc.last.kw["cwd"], "/work")
        self.assertNotIn("ANTHROPIC_API_KEY", FakeProc.last.kw["env"])


class SpeechTest(unittest.TestCase):
    def test_clean_for_speech(self):
        t = speech.clean_for_speech("## 結果\n- **Data** が 3MB\n- 詳しくは https://example.com\n[[音声連携]] を見て")
        self.assertEqual(t, "結果。データが3メガバイト。詳しくはリンク。音声連携を見て")
        self.assertEqual(speech.clean_for_speech("```py\nprint(1)\n```"), "コードは画面で見てください")
        self.assertEqual(speech.clean_for_speech("  \n "), "")

    def test_speaker_order_and_stop(self):
        said = []

        class P:
            def __init__(self, cmd):
                said.append(cmd[-1])

            def wait(self):
                return 0

            def poll(self):
                return 0

        s = speech.Speaker(cfg(), popen=P)
        s.say("一つ目")
        s.say("**二つ目**")
        self.assertTrue(s.wait(timeout=2))
        self.assertEqual(said, ["一つ目", "二つ目"])
        s.stop()
        self.assertTrue(s.wait(timeout=1))


class SttTest(unittest.TestCase):
    def test_clean_transcript(self):
        self.assertEqual(stt.clean_transcript(" 今日の予定は？ "), "今日の予定は？")
        self.assertEqual(stt.clean_transcript("ご視聴ありがとうございました。"), "")
        self.assertEqual(stt.clean_transcript("[音楽]"), "")
        self.assertEqual(stt.clean_transcript("(拍手) Inbox 読んで"), "Inbox 読んで")

    def test_whisper_cpp(self):
        c = cfg()
        c["stt"]["engine"] = "whisper_cpp"
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=" Inbox に牛乳を追加して\n", stderr="")

        self.assertEqual(stt.transcribe("/x.wav", c, run=run), "Inbox に牛乳を追加して")
        self.assertIn("-l", calls[0])
        self.assertEqual(calls[0][calls[0].index("-l") + 1], "ja")


class AudioTest(unittest.TestCase):
    def test_silence_detector(self):
        d = SilenceDetector(0.1, threshold=500, silence_sec=0.3, max_sec=5, wait_sec=1)
        self.assertFalse(d.feed(1000))
        self.assertFalse(d.feed(100))
        self.assertFalse(d.feed(100))
        self.assertTrue(d.feed(100))  # 話したあと 0.3 秒黙った

    def test_silence_detector_gives_up_and_caps(self):
        d = SilenceDetector(0.5, wait_sec=1)
        self.assertFalse(d.feed(0))
        self.assertTrue(d.feed(0))
        d = SilenceDetector(1, max_sec=2)
        self.assertFalse(d.feed(9999))
        self.assertTrue(d.feed(9999))

    def test_rms_wav_duration(self):
        self.assertEqual(rms([]), 0.0)
        self.assertAlmostEqual(rms([3, -3]), 3.0)
        frames = b"\x00\x00" * RATE
        self.assertAlmostEqual(duration(frames), 1.0)
        with tempfile.TemporaryDirectory() as d:
            p = save_wav(Path(d) / "a.wav", frames)
            self.assertGreater(p.stat().st_size, 44)


class CmdLogTest(unittest.TestCase):
    def test_write_qa_matches_voice_to_md(self):
        with tempfile.TemporaryDirectory() as d:
            now = datetime(2026, 11, 15, 9, 5, 7, tzinfo=JST)
            p1 = cmdlog.write_qa(d, "質問", "回答", now)
            p2 = cmdlog.write_qa(d, "質問2", "回答2", now)
            self.assertEqual(p1.name, "2026-11-15-090507.json")
            self.assertEqual(p2.name, "2026-11-15-090507-2.json")
            data = json.loads(p1.read_text(encoding="utf-8"))
            self.assertEqual(data["datetime"], "2026-11-15T09:05:07+09:00")
            self.assertEqual((data["question"], data["answer"]), ("質問", "回答"))

    def test_push_commits_only_the_log(self):
        def git(*a, cwd):
            subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)

        with tempfile.TemporaryDirectory() as d:
            remote, work = Path(d) / "remote.git", Path(d) / "work"
            git("init", "-q", "--bare", "-b", "main", str(remote), cwd=d)
            git("clone", "-q", str(remote), str(work), cwd=d)
            for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
                git("config", k, v, cwd=work)
            (work / "Inbox.md").write_text("# Inbox\n")
            git("add", ".", cwd=work)
            git("commit", "-q", "-m", "init", cwd=work)
            git("push", "-q", "-u", "origin", "main", cwd=work)
            (work / "Inbox.md").write_text("# Inbox\n- [ ] 書きかけ\n")  # まだコミットしていない変更
            p = cmdlog.write_qa(work, "q", "a")
            self.assertTrue(cmdlog.push(work, p))
            files = subprocess.run(["git", "show", "--name-only", "--format=", "HEAD"], cwd=work,
                                   capture_output=True, text=True).stdout.split()
            self.assertEqual(files, [f"Data/ray/{p.name}"])  # デイリーに書き足す Data/voice/ には置かない
            self.assertIn("書きかけ", (work / "Inbox.md").read_text())  # 書きかけは残る
            remote_head = subprocess.run(["git", "rev-parse", "main"], cwd=remote, capture_output=True, text=True).stdout
            local_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True).stdout
            self.assertEqual(remote_head, local_head)


class FakeBrain:
    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.cancelled = 0

    def ask(self, text, on_text, session_id=None, confirmed=False):
        self.calls.append({"text": text, "session_id": session_id, "confirmed": confirmed})
        a = self.answers.pop(0)
        on_text(a)
        return brain.Result(answer=a, session_id="sess", spoken=[a])

    def cancel(self):
        self.cancelled += 1


class FakeSpeaker:
    def __init__(self):
        self.said = []
        self.stopped = 0

    def say(self, t):
        self.said.append(t)

    def stop(self):
        self.stopped += 1

    def wait(self, timeout=None):
        return True


class AssistantTest(unittest.TestCase):
    def make(self, answers, now=[0.0]):
        self.now = [0.0]
        self.logs = []
        b, s, hub = FakeBrain(answers), FakeSpeaker(), StateHub()
        a = Assistant(cfg(), b, s, hub, log=lambda c, q, ans, **kw: self.logs.append((q, ans)),
                      clock=lambda: self.now[0])
        return a, b, s, hub

    def test_conversation_continues_then_times_out(self):
        a, b, s, hub = self.make(["一つ目", "二つ目", "三つ目"])
        a.handle_text("今日のデイリー読んで")
        self.now[0] = 100
        a.handle_text("その中で一番大事なのは？")
        self.now[0] = 1000
        a.handle_text("Inbox は？")
        self.assertEqual([c["session_id"] for c in b.calls], [None, "sess", None])
        self.assertEqual(s.said, ["一つ目", "二つ目", "三つ目"])
        self.assertEqual(len(self.logs), 3)
        self.assertEqual(hub.state["state"], "idle")

    def test_confirm_yes(self):
        a, b, s, hub = self.make(["確認です。node_modules を消します。よいですか？", "消しました"])
        a.handle_text("node_modules 消して")
        self.assertTrue(a.awaiting_confirm)
        self.assertEqual(hub.state["state"], "confirm")
        self.now[0] = 9999  # 時間がたっても確認の続きは同じ会話で
        a.handle_text("はい")
        self.assertEqual(b.calls[1], {"text": CONFIRMED_PROMPT, "session_id": "sess", "confirmed": True})
        self.assertFalse(a.awaiting_confirm)

    def test_confirm_no(self):
        a, b, s, hub = self.make(["確認です。push します。よいですか？"])
        a.handle_text("push して")
        a.handle_text("いや、やめて")
        self.assertEqual(len(b.calls), 1)  # Claude は呼ばない
        self.assertEqual(s.said[-1], "やめておきますね")
        self.assertFalse(a.awaiting_confirm)

    def test_confirm_other_question_is_not_confirmed(self):
        a, b, s, hub = self.make(["確認です。消します。よいですか？", "晴れです"])
        a.handle_text("消して")
        a.handle_text("ところで天気は？")
        self.assertFalse(b.calls[1]["confirmed"])

    def test_reset_and_empty(self):
        a, b, s, hub = self.make(["一つ目", "二つ目"])
        a.handle_text("今日の予定は？")
        a.handle_text("新しい話にして")
        a.handle_text("Inbox は？")
        self.assertIsNone(b.calls[1]["session_id"])
        a.handle_text("  ")
        self.assertEqual(s.said[-1], "聞き取れませんでした")

    def test_handle_audio(self):
        a, b, s, hub = self.make(["はい"])
        a.transcribe = lambda wav, c: "今日の予定は？"
        a.handle_audio(b"\x00\x00" * 100)  # 短すぎる録音は捨てる
        self.assertEqual(b.calls, [])
        a.handle_audio(b"\x00\x00" * RATE)
        self.assertEqual(b.calls[0]["text"], "今日の予定は？")
        self.assertFalse(a.busy.is_set())

    def test_interrupt(self):
        a, b, s, hub = self.make([])
        a.interrupt()
        self.assertEqual((s.stopped, b.cancelled, hub.state["state"]), (1, 1, "listening"))


class ConfigTest(unittest.TestCase):
    def test_merge(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "c.toml"
            p.write_text('cmd_dir = "~/x"\n[tts]\nvoice = "Otoya"\n')
            c = config.load(p)
            self.assertEqual(c["tts"], {"voice": "Otoya", "rate": 210, "lipsync": True})
            self.assertTrue(c["cmd_dir"].endswith("/x") and not c["cmd_dir"].startswith("~"))


if __name__ == "__main__":
    unittest.main()


class VoicePromptTest(unittest.TestCase):
    """話し方の決まり（6文まで・記号なし・日本語だけ）が、画面の決まりとぶつからないこと"""

    def test_voice_rules(self):
        p = brain.VOICE_PROMPT
        self.assertIn("全部で6文まで", p)
        self.assertIn("使ってよい記号は「、」「。」「？」だけ", p)
        self.assertIn("アルファベットは一文字も書かない", p)
        self.assertIn("コマンド（Cmd）", p)

    def test_screen_rules_fit_three_sentences(self):
        p = brain.SCREEN_PROMPT
        self.assertIn("場面は多くて4つ", p)
        self.assertIn("話す文を足して6文まで", p)
        self.assertNotIn("多くて6つ", p)
        self.assertIn("返事には毎回", p)  # 毎回、画面をつける
        self.assertNotIn("todos(due_within 3) → habits", p)

    def test_prompt_is_sent(self):
        cmd = brain.build_command(cfg())
        sent = cmd[cmd.index("--append-system-prompt") + 1]
        self.assertIn("全部で6文まで", sent)
        self.assertIn("場面は多くて4つ", sent)


class GuardTest(unittest.TestCase):
    def test_to_japanese(self):
        from ray.guard import to_japanese as j
        self.assertEqual(j("中身はAnthropicのClaudeで、Cmdリポジトリのノートを読みます。"),
                         "中身はアンソロピックのクロードで、コマンドリポジトリのノートを読みます。")
        self.assertEqual(j("kakei.jsonという名前で、Money Forward MEから読みます。"),
                         "ファイルという名前で、マネーフォワードから読みます。")
        self.assertEqual(j("上半身Aは18:45から。体重70.5kg、目標まで3%です！"),
                         "上半身エーは18時45分から。体重70.5キロ、目標まで3パーセントです。")
        self.assertEqual(j("「エッチマス」は2026-11-20と11/20に。API は OK ?"),
                         "エッチマスは2026年11月20日と11月20日に。エーピーアイはオッケー？")
        self.assertEqual(j("家賃は¥85,000で、残りは 1〜2 万円 → https://x.example/a を見て"),
                         "家賃は85,000円で、残りは1から2万円、リンクを見て")

    def test_budget(self):
        from ray.guard import Budget, join
        b = Budget(3)
        self.assertEqual(b.take("調べます。"), "調べます。")  # 最初の短いひとことは数えない
        self.assertEqual(b.left, 3)
        self.assertEqual(b.take("一。二？"), "一。二？")
        self.assertEqual(b.take("三。四。"), "三。")
        self.assertEqual(b.take("五。"), "")
        self.assertEqual(b.rest, ["四。", "五。"])
        self.assertTrue(b.overflow)
        self.assertEqual(join(["調べます", "今週は2つ。", " 多めです。"]), "調べます。今週は2つ。多めです。")

    def test_log_extra(self):
        with tempfile.TemporaryDirectory() as d:
            p = cmdlog.write_qa(d, "q", "a", extra={"raw": "@画面 todos {}\na", "trimmed": True})
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertEqual((data["raw"], data["trimmed"]), ("@画面 todos {}\na", True))
            p = cmdlog.write_qa(d, "q", "a", extra={"raw": "", "trimmed": False})
            data = json.loads(p.read_text(encoding="utf-8"))
            self.assertNotIn("raw", data)  # 同じなら残さない
            self.assertNotIn("trimmed", data)


class PushToTalkTest(unittest.TestCase):
    """キーの知らせの中では何もしない（遅いと macOS がキーの監視を止め、離したのが届かなくなる）"""

    def make(self, max_hold=30.0, slow_start=0.0):
        import threading
        import time

        from ray.__main__ import PushToTalk

        events, done = [], threading.Event()

        class A:
            hub = StateHub()
            def interrupt(self): events.append("interrupt")
            def submit_audio(self, frames): events.append(("audio", frames)); done.set()

        class R:
            def start(self): time.sleep(slow_start); events.append("start")
            def stop(self): events.append("stop"); return b"xx"

        return PushToTalk(A(), R(), max_hold=max_hold), events, done

    def test_press_returns_at_once_even_if_mic_is_slow(self):
        import time

        ptt, events, done = self.make(slow_start=0.5)
        t = time.monotonic()
        ptt.press()
        ptt.press()  # 押しっぱなしの繰り返しは捨てる
        ptt.release()
        self.assertLess(time.monotonic() - t, 0.1)
        self.assertTrue(done.wait(3))
        self.assertEqual(events, ["interrupt", "start", "stop", ("audio", b"xx")])

    def test_lost_release_is_cut_after_max_hold(self):
        ptt, events, done = self.make(max_hold=0.2)
        ptt.press()
        self.assertTrue(done.wait(3))
        self.assertEqual(events[-1], ("audio", b"xx"))
        ptt.press()  # 次はまた押せる
        self.assertEqual(ptt.pressed, True)
