"""Claude の返事を文ごとに話す・場面の間をあけない"""
import json
import threading
import unittest

from ray import brain, config, speech
from ray.app import Assistant
from ray.ui import StateHub
from tests.test_ray import FakeSpeaker, cfg
from tests.test_speech import FakeProc, write_wav


def delta(text):
    return json.dumps({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0,
                                                          "delta": {"type": "text_delta", "text": text}}})


def ev(kind):
    return json.dumps({"type": "stream_event", "event": {"type": kind}})


def assistant(text):
    return json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}})


class PartialStreamTest(unittest.TestCase):
    def test_speaks_each_sentence_once_as_it_arrives(self):
        got = []
        full = '@画面 steps {"title":"手順。です","steps":["a"]}\n1つ目です。2つ目'
        lines = [ev("content_block_start"), delta('@画面 steps {"title":"手順。'), delta('です","steps":["a"]}\n1つ目'),
                 delta("です。2つ目")]

        def gen():
            for l in lines:
                yield l
            self.assertEqual(got, ['@画面 steps {"title":"手順。です","steps":["a"]}\n1つ目です。'])  # 全部を待たずに1文目
            yield delta("です！")
            yield assistant(full + "です！")    # 同じ中身がまとめて来ても、もう一度は言わない
            yield ev("content_block_stop")
            yield json.dumps({"type": "result", "subtype": "success", "result": full + "です！"})

        res = brain.parse_stream(gen(), got.append)
        self.assertEqual(got, ['@画面 steps {"title":"手順。です","steps":["a"]}\n1つ目です。', "2つ目です！"])
        self.assertEqual(res.answer, full + "です！")

    def test_last_sentence_without_period_and_tool_turns(self):
        got = []
        lines = [ev("content_block_start"), delta("調べます"), assistant("調べます"), ev("content_block_stop"),
                 json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Read"}]}}),
                 ev("content_block_start"), delta("Data です"), assistant("Data です"), ev("content_block_stop"),
                 json.dumps({"type": "result", "subtype": "success", "result": "Data です"})]
        res = brain.parse_stream(lines, got.append)
        self.assertEqual(got, ["調べます", "Data です"])
        self.assertEqual(res.usage["tools"], ["Read"])

    def test_without_partial_messages_still_works(self):
        got = []
        brain.parse_stream([assistant("一つ目。二つ目。"), json.dumps({"type": "result", "subtype": "success", "result": "一つ目。二つ目。"})],
                           got.append)
        self.assertEqual(got, ["一つ目。二つ目。"])

    def test_command_asks_for_partial_messages(self):
        self.assertIn("--include-partial-messages", brain.build_command(cfg()))


class OnStartTest(unittest.TestCase):
    def test_next_voice_is_made_while_current_plays_and_picture_follows_voice(self):
        events, hold = [], threading.Event()

        def popen(cmd):
            if cmd[0] == "say" and "-o" in cmd:
                write_wav(cmd[cmd.index("-o") + 1], [(0.05, 1.0)])
                events.append(("synth", cmd[-1]))
                return FakeProc(0)
            events.append(("play", ""))
            return FakeProc(hold=hold if len([e for e in events if e[0] == "play"]) == 1 else None)

        s = speech.Speaker(config.load("/nonexistent.toml"), popen=popen, on_voice=lambda *a: None,
                           which=lambda x: "/usr/bin/" + x)
        s.say("一つ目。", on_start=lambda: events.append(("show", 1)))
        s.say("", on_start=lambda: events.append(("show", 2)))  # 声のない場面
        s.say("三つ目。", on_start=lambda: events.append(("show", 3)))
        for _ in range(100):
            if ("synth", "三つ目。") in events:
                break
            threading.Event().wait(.02)
        self.assertIn(("synth", "三つ目。"), events)           # 1つ目を鳴らしている間に、3つ目の声ができている
        self.assertNotIn(("show", 2), events)                  # 画面は、まだ1つ目のまま
        hold.set()
        self.assertTrue(s.wait(timeout=3))
        order = [e for e in events if e[0] in ("show", "play")]
        self.assertEqual(order, [("show", 1), ("play", ""), ("show", 2), ("show", 3), ("play", "")])


class PlayTest(unittest.TestCase):
    def test_play_queues_all_scenes_and_streamed_sentences_join_the_scene(self):
        class B:
            def ask(self, text, on_text, session_id=None, confirmed=False):
                on_text("@画面 todos\nひとつめ。")
                on_text("続きの文。")
                on_text("@画面 habits\nふたつめ。")
                return brain.Result(answer="ひとつめ。続きの文。ふたつめ。")

            def cancel(self): pass

        s = FakeSpeaker()
        a = Assistant(cfg(), B(), s, StateHub(), log=lambda *x, **k: None)
        a.handle_text("やることは？")
        self.assertEqual(s.said, ["ひとつめ。", "続きの文。", "ふたつめ。"])
        self.assertEqual([sc.speech for sc in a.deck], ["ひとつめ。続きの文。", "ふたつめ。"])
        self.assertEqual(a.pos, 1)


if __name__ == "__main__":
    unittest.main()
