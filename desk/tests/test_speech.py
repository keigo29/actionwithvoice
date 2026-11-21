"""読み上げ：声を書き出して大きさを測り、鳴らし始めに画面へ送る（発音に合わせて円の内側を光らせる）"""

import array
import io
import json
import math
import tempfile
import threading
import unittest
import wave
from pathlib import Path

from ray import config, speech


class FakeResp(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def write_wav(path, parts, rate=22050):
    """parts：[(秒, 大きさ0〜1)]。その長さの正弦波を順に書く"""
    data = array.array("h")
    for sec, amp in parts:
        for i in range(int(sec * rate)):
            data.append(int(amp * 30000 * math.sin(i * 2 * math.pi * 220 / rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(data.tobytes())


class EnvelopeTest(unittest.TestCase):
    def test_loud_and_quiet_parts(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "v.wav"
            write_wav(p, [(0.2, 1.0), (0.2, 0.0), (0.2, 0.25)])
            env = speech.envelope(str(p))
        self.assertEqual(len(env), 15)              # 0.6秒 ÷ 40ミリ秒
        self.assertEqual(max(env[:5]), 1.0)         # いちばん大きいところが1
        self.assertEqual(max(env[5:10]), 0.0)       # 無音
        self.assertTrue(0.2 < env[12] < 0.5)        # 小さい声も少し持ち上げて見える


class FakeProc:
    def __init__(self, code=0, hold=None):
        self.code, self.hold, self.killed = code, hold, False

    def wait(self):
        if self.hold:
            self.hold.wait(2)
        return self.code

    def poll(self):
        return None if self.hold and not self.hold.is_set() else self.code

    def terminate(self):
        self.killed = True
        if self.hold:
            self.hold.set()


class LipsyncTest(unittest.TestCase):
    def make(self, synth_ok=True, hold=None):
        self.events = []
        self.voices = []

        def popen(cmd):
            if cmd[0] == "say" and "-o" in cmd:
                if synth_ok:
                    write_wav(cmd[cmd.index("-o") + 1], [(0.08, 1.0), (0.08, 0.1)])
                self.events.append(("synth", cmd[-1]))
                return FakeProc(0 if synth_ok else 1)
            self.events.append((cmd[0], cmd[-1]))
            return FakeProc(hold=hold)

        s = speech.Speaker(config.load("/nonexistent.toml"), popen=popen,
                           on_voice=lambda env, step: self.voices.append((env, step)), which=lambda x: "/usr/bin/" + x)
        return s

    def test_writes_measures_then_plays_in_order(self):
        s = self.make()
        s.say("一つ目。")
        s.say("二つ目。")
        self.assertTrue(s.wait(timeout=3))
        played = [e for e in self.events if e[0] == "afplay"]
        self.assertEqual(len(played), 2)
        self.assertEqual([e[1] for e in self.events if e[0] == "synth"], ["一つ目。", "二つ目。"])
        self.assertEqual(len(self.voices), 2)       # 鳴らすたびに、声の大きさを送る
        env, step = self.voices[0]
        self.assertEqual(step, speech.LIPSYNC_STEP_MS)
        self.assertEqual(env[0], 1.0)
        self.assertLess(env[-1], 0.5)

    def test_falls_back_to_say_when_writing_fails(self):
        s = self.make(synth_ok=False)
        s.say("読みます。")
        self.assertTrue(s.wait(timeout=3))
        self.assertIn(("say", "読みます。"), self.events)
        self.assertEqual(self.voices, [])

    def test_stop_cuts_current_and_waiting(self):
        hold = threading.Event()
        s = self.make(hold=hold)
        s.say("長い文。")
        s.say("次の文。")
        for _ in range(100):
            if any(e[0] == "afplay" for e in self.events):
                break
            threading.Event().wait(.02)
        s.stop()
        self.assertTrue(s.wait(timeout=1))
        hold.set()
        threading.Event().wait(.2)
        self.assertEqual(len([e for e in self.events if e[0] == "afplay"]), 1)  # 次の文は鳴らさない

    def test_without_on_voice_speaks_directly(self):
        said = []

        def popen(cmd):
            said.append(cmd)
            return FakeProc()

        s = speech.Speaker(config.load("/nonexistent.toml"), popen=popen, which=lambda x: "/usr/bin/" + x)
        s.say("そのまま。")
        self.assertTrue(s.wait(timeout=2))
        self.assertEqual(said[0][0], "say")
        self.assertNotIn("-o", said[0])

    def test_can_turn_off(self):
        c = config.load("/nonexistent.toml")
        c["tts"]["lipsync"] = False
        s = speech.Speaker(c, on_voice=lambda *a: None, which=lambda x: "/usr/bin/" + x, popen=lambda cmd: FakeProc())
        self.assertFalse(s.lipsync)


class FakeVoicevox:
    """VOICEVOX のエンジンの代わり（audio_query → synthesis）"""

    def __init__(self, up=True):
        self.up = up
        self.calls = []

    def __call__(self, req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        self.calls.append(url)
        if not self.up:
            raise OSError("connection refused")
        if url.endswith("/version"):
            return FakeResp(b'"0.22.0"')
        if "/audio_query" in url:
            return FakeResp(json.dumps({"speedScale": 1.0, "accent_phrases": []}).encode())
        if "/synthesis" in url:
            self.query = json.loads(req.data)
            path = tempfile.mktemp(suffix=".wav")
            write_wav(path, [(0.08, 1.0), (0.08, 0.2)])
            return FakeResp(Path(path).read_bytes())
        raise AssertionError(url)


class ReadingTest(unittest.TestCase):
    def test_ha_iru_is_read_as_wa(self):
        self.assertEqual(speech.fix_reading("傘はいりません。"), "傘は要りません。")
        self.assertEqual(speech.fix_reading("コートはいらないです"), "コートは要らないです")
        self.assertEqual(speech.fix_reading("猫はいる。"), "猫はいる。")          # 居る：変えない
        self.assertEqual(speech.fix_reading("部屋にはいります"), "部屋にはいります")  # 入る：変えない
        self.assertEqual(speech.fix_reading("今日は晴れ"), "今日は晴れ")


class VoicevoxTest(unittest.TestCase):
    def make(self, up=True):
        c = config.load("/nonexistent.toml")
        c["tts"]["engine"] = "voicevox"
        self.events, self.voices = [], []
        self.vv = FakeVoicevox(up)

        def popen(cmd):
            self.events.append(cmd)
            if cmd[0] == "say" and "-o" in cmd:
                write_wav(cmd[cmd.index("-o") + 1], [(0.08, 1.0)])
            return FakeProc()

        return speech.Speaker(c, popen=popen, on_voice=lambda env, step: self.voices.append(env),
                              which=lambda x: "/usr/bin/" + x, urlopen=self.vv)

    def test_speaks_with_voicevox(self):
        s = self.make()
        s.say("こんにちは。")
        self.assertTrue(s.wait(timeout=3))
        self.assertTrue(any("/audio_query?" in u and "speaker=3" in u for u in self.vv.calls))
        self.assertEqual(self.vv.query["speedScale"], 1.15)
        self.assertEqual([c[0] for c in self.events], ["afplay"])   # say は使わない
        self.assertEqual(len(self.voices), 1)                       # 画面の光も動く

    def test_falls_back_to_say_and_starts_app_when_down(self):
        import sys
        from unittest import mock
        with mock.patch.object(sys, "platform", "darwin"):
            s = self.make(up=False)
            s.say("読みます。")
            self.assertTrue(s.wait(timeout=3))
        threading.Event().wait(.1)
        self.assertIn(["open", "-g", "-a", "VOICEVOX"], self.events)
        self.assertTrue(any(c[0] == "say" and "-o" in c for c in self.events))
        self.assertTrue(any(c[0] == "afplay" for c in self.events))


if __name__ == "__main__":
    unittest.main()


class EchoTest(unittest.TestCase):
    def test_add_echo_overlays_quieter_copies(self):
        import array as _array
        import tempfile as _tempfile
        import wave as _wave
        from pathlib import Path as _Path
        with _tempfile.TemporaryDirectory() as d:
            p = str(_Path(d) / "v.wav")
            with _wave.open(p, "wb") as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(1000)
                w.writeframes(_array.array("h", [10000] + [0] * 99).tobytes())
            self.assertTrue(speech.add_echo(p, delay_ms=10, decay=0.5, repeats=2))
            with _wave.open(p, "rb") as w:
                out = _array.array("h", w.readframes(w.getnframes()))
            self.assertEqual(len(out), 120)  # 響きの分だけ長くなる
            self.assertEqual((out[0], out[10], out[20]), (10000, 5000, 2500))
