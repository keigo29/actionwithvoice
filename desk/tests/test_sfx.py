import tempfile
import threading
import unittest
import wave
from pathlib import Path

from ray import sfx
from ray.app import Assistant
from ray.ui import StateHub
from tests.test_screen import FakeBrain, FakeSpeaker, cfg, fake_cmd


class RenderTest(unittest.TestCase):
    def test_render_all(self):
        with tempfile.TemporaryDirectory() as d:
            paths = sfx.render(Path(d))
            self.assertEqual(set(paths), set(sfx.NAMES))
            lengths = {}
            for name, p in paths.items():
                with wave.open(str(p)) as w:
                    self.assertEqual((w.getnchannels(), w.getsampwidth(), w.getframerate()), (2, 2, 44100))
                    lengths[name] = w.getnframes() / 44100
                    frames = w.readframes(w.getnframes())
                peak = max(abs(int.from_bytes(frames[i:i + 2], "little", signed=True)) for i in range(0, len(frames), 2))
                self.assertGreater(peak, 20000)  # 小さすぎない
                self.assertLess(peak, 32767)     # 割れない
            self.assertGreater(lengths["boot"], 2.0)  # 起動はたっぷり
            self.assertEqual(max(lengths, key=lengths.get), "boot")
            self.assertLess(lengths["listen"], lengths["open"])  # 合図は、画面を開く音より短く
            self.assertLess(lengths["ack"], lengths["open"])
            self.assertLess(lengths["open"], 1.6)


class SoftTest(unittest.TestCase):
    """「カチッ」ではなく「ファン」：始まりが鋭くないこと"""

    def test_soft_attack(self):
        with tempfile.TemporaryDirectory() as d:
            for name, p in sfx.render(Path(d)).items():
                with wave.open(str(p)) as w:
                    raw = w.readframes(w.getnframes())
                left = [int.from_bytes(raw[i:i + 2], "little", signed=True) for i in range(0, len(raw), 4)]
                peak = max(abs(v) for v in left)
                first = max(abs(v) for v in left[: int(0.008 * 44100)])  # 最初の 8 ミリ秒
                self.assertLess(first, peak * 0.3, name)


class SfxTest(unittest.TestCase):
    def test_plays_with_afplay(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            s = sfx.Sfx({"sfx": {"enabled": True, "volume": 0.3}}, popen=lambda cmd, **kw: calls.append(cmd),
                        which=lambda b: "/usr/bin/afplay", folder=Path(d))
            self.assertTrue(s.ready.wait(20))
            s.play("open")
            self.assertEqual(calls[0][:3], ["afplay", "-v", "0.3"])
            self.assertTrue(calls[0][3].endswith("open.wav"))
            # 2回目の起動では、作っておいた音をそのまま使う（作り直さない）
            mtime = (Path(d) / "boot.wav").stat().st_mtime_ns
            s2 = sfx.Sfx({"sfx": {"enabled": True}}, popen=lambda cmd, **kw: None,
                         which=lambda b: "/usr/bin/afplay", folder=Path(d))
            self.assertTrue(s2.ready.wait(5))
            self.assertEqual((Path(d) / "boot.wav").stat().st_mtime_ns, mtime)

    def test_off_without_afplay_or_when_disabled(self):
        calls = []
        sfx.Sfx({"sfx": {"enabled": True}}, popen=lambda cmd, **kw: calls.append(cmd), which=lambda b: None).play("boot")
        sfx.Sfx({"sfx": {"enabled": False}}, popen=lambda cmd, **kw: calls.append(cmd),
                which=lambda b: "/usr/bin/afplay").play("boot")
        self.assertEqual(calls, [])


class FakeSfx:
    def __init__(self):
        self.played = []

    def play(self, name, wait=0.0):
        self.played.append(name)


class WhenToPlayTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake_cmd(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, answers):
        self.fx = FakeSfx()
        a = Assistant(cfg(cmd_dir=self.tmp.name), FakeBrain(answers), FakeSpeaker(), StateHub(),
                      log=lambda *x, **kw: None, sfx=self.fx)
        a.shot = lambda url, quote="": None
        return a

    def test_listen_ack_open_close(self):
        ans = ('@画面 calendar {"range":"week"}\n今週は3つです。\n'
               '@画面 calendar {"range":"week","highlight":["飲み会"]}\n今日は飲み会です。\n'
               '@画面 todos {}\nやることは2つです。')
        a = self.make([ans, "はい。"])
        a.transcribe = lambda wav, c: "今週の予定は？"
        a.interrupt()
        a.handle_audio(b"\x00\x00" * 16000)
        # 聞き始め → 聞き取れた → 画面を開く（カレンダー）→ 同じ種類の間は鳴らさない → やることで開く
        self.assertEqual(self.fx.played, ["listen", "ack", "open", "open"])
        a.handle_text("閉じて")
        self.assertEqual(self.fx.played[-1], "close")
        a.handle_text("ありがとう")  # 画面のない返事では鳴らさない
        self.assertEqual(self.fx.played[-1], "close")

    def test_no_ack_when_nothing_heard(self):
        a = self.make([])
        a.transcribe = lambda wav, c: ""
        a.handle_audio(b"\x00\x00" * 16000)
        self.assertNotIn("ack", self.fx.played)

    def test_confirm_card_opens(self):
        a = self.make(["確認です。消します。よいですか？"])
        a.handle_text("消して")
        self.assertEqual(self.fx.played, ["open"])


if __name__ == "__main__":
    unittest.main()
