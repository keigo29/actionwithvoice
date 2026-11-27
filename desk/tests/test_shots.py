import tempfile
import threading
import unittest
from pathlib import Path

from ray import shots


class FakePage:
    def __init__(self, log):
        self.log = log
    def goto(self, url, **kw): self.log.append(("goto", url))
    def wait_for_timeout(self, ms): pass
    def evaluate(self, js, q): self.log.append(("mark", q))
    def screenshot(self, path): Path(path).write_bytes(b"png")
    def close(self): self.log.append(("close_page",))


class FakeBrowser:
    def __init__(self, log):
        self.log = log
    def new_page(self, **kw): return FakePage(self.log)
    def is_connected(self): return True


class ShooterTest(unittest.TestCase):
    def test_one_browser_for_many_shots_and_closes_when_idle(self):
        log, stopped = [], threading.Event()
        s = shots.Shooter(launch=lambda: (FakeBrowser(log), stopped.set), idle_sec=0.3)
        with tempfile.TemporaryDirectory() as d:
            for i in range(3):
                self.assertTrue(s.shoot(f"https://example.com/{i}", Path(d) / f"{i}.png", quote="引用" if i == 0 else ""))
            self.assertEqual(s.launches, 1)                   # ブラウザの起動は1回だけ
            self.assertEqual(log.count(("close_page",)), 3)   # ページは毎回閉じる
            self.assertIn(("mark", "引用"), log)
            self.assertTrue(stopped.wait(2))                  # 使わなくなったら閉じる
            self.assertTrue(s.shoot("https://example.com/again", Path(d) / "x.png"))
            self.assertEqual(s.launches, 2)                   # また頼まれたら起動し直す

    def test_failure_returns_false(self):
        def launch():
            raise RuntimeError("起動できない")
        s = shots.Shooter(launch=launch, idle_sec=0.2)
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(s.shoot("https://example.com/", Path(d) / "a.png"))


if __name__ == "__main__":
    unittest.main()
