import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ray import actions, routine
from ray.app import Assistant
from ray.ui import StateHub
from tests.test_ray import FakeBrain, FakeSpeaker, cfg

JST = dt.timezone(dt.timedelta(hours=9))
WEATHER = {"place": "横浜", "date": "2026-11-27", "max": 15, "min": 7, "pop": 60, "title": "横浜の天気",
           "hours": [{"time": f"{h:02d}:00", "temp": 10, "pop": 60 if h == 15 else 0, "sky": "くもり"} for h in range(24)]}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "Inbox.md").write_text("# Inbox\n- [ ] 資料を送る 📅 2026-11-28\n", encoding="utf-8")
        self.daily = self.root / "2026" / "11" / "27" / "デイリー.md"
        self.daily.parent.mkdir(parents=True)
        self.daily.write_text("## 予定\n- 10:00 会議\n- 19:00 飲み会\n## メモ\n", encoding="utf-8")
        self.c = cfg(cmd_dir=str(self.root))
        self.c["actions"] = {"enabled": True, "push": False}
        self.t = [dt.datetime(2026, 11, 27, 7, 0, tzinfo=JST)]
        self.idle = [10.0]
        self.clock = [0.0]
        self.s = FakeSpeaker()
        self.b = FakeBrain(["Claude の答え"])
        self.logs = []
        self.a = Assistant(self.c, self.b, self.s, StateHub(), log=lambda c, q, ans, extra=None: self.logs.append((q, ans, extra)),
                           clock=lambda: self.clock[0])
        self.a.actions = actions.Actions(self.c, now=lambda: self.t[0], pull=lambda r: None, push=lambda *x: None)
        self.r = routine.Routine(self.c, self.a, now=lambda: self.t[0], idle=lambda: self.idle[0],
                                 state=routine.State(self.root / "state.json"), clock=lambda: self.clock[0])
        self.a.routine = self.r
        self.patch = mock.patch("ray.visuals.weather", return_value=WEATHER)
        self.patch.start()
        visuals_today = mock.patch("ray.visuals.today", return_value=dt.date(2026, 11, 27))
        visuals_today.start()
        self.addCleanup(visuals_today.stop)

    def tearDown(self):
        self.patch.stop()
        self.tmp.cleanup()

    def at(self, h, m=0):
        self.t[0] = self.t[0].replace(hour=h, minute=m)


class MorningTest(Base):
    def test_brief_once_when_present_then_asks_sleep(self):
        self.idle[0] = 999                       # 机にいない：話さない
        self.assertIsNone(self.r.tick())
        self.idle[0] = 5
        self.assertEqual(self.r.tick(), "morning")
        said = " ".join(self.s.said)
        self.assertIn("最高15度、最低7度です。雨の確率は60パーセントです。傘を持っていくと安心です。", said)
        self.assertIn("今日の予定は2つです。", said)
        self.assertIn("期限が3日以内のやることは1つです。いちばん近いのは、資料を送るです。", said)
        self.assertIn(routine.SLEEP_Q, said)
        self.assertEqual(self.b.calls, [])        # Claude は呼ばない
        self.assertEqual(self.a.expect[0], "sleep")
        self.assertNotEqual(self.r.tick(), "morning")  # 同じ日は1回だけ
        # 答え（ボタンで答えた）→ 記録
        self.a.handle_text("2時に寝て8時に起きた")
        self.assertIn("- 睡眠 2:00-8:00 6時間", self.daily.read_text(encoding="utf-8"))
        self.assertEqual(self.b.calls, [])

    def test_not_outside_hours_and_remembered_after_restart(self):
        self.at(12)
        self.assertIsNone(self.r.tick())
        self.at(7)
        self.r.tick()
        again = routine.Routine(self.c, self.a, now=lambda: self.t[0], idle=lambda: 1, state=routine.State(self.root / "state.json"))
        self.assertNotEqual(again.tick(), "morning")

    def test_no_sleep_question_when_already_written(self):
        self.daily.write_text("## メモ\n- 睡眠 1:00-7:00 6時間\n", encoding="utf-8")
        self.r.tick()
        self.assertNotIn(routine.SLEEP_Q, " ".join(self.s.said))
        self.assertIsNone(self.a.expect)

    def test_ohayo_gives_brief_without_claude(self):
        self.a.handle_text("おはよう")
        self.assertIn("今日の予定は2つです。", " ".join(self.s.said))
        self.assertEqual(self.b.calls, [])

    def test_answer_that_is_not_an_answer_goes_on_normally(self):
        self.r.tick()
        self.a.handle_text("家計の残りはいくら？")
        self.assertEqual(len(self.b.calls), 1)
        self.assertIsNone(self.a.expect)


class RemindTest(Base):
    def setUp(self):
        super().setUp()
        self.r.state.mark("2026-11-27", "morning")

    def test_reminds_ten_minutes_before_once(self):
        self.at(9, 45)
        self.assertIsNone(self.r.tick())
        self.at(9, 52)
        self.assertEqual(self.r.tick(), "remind")
        self.assertEqual(self.s.said[-1], "あと8分で、10時から、会議です。")
        self.assertIsNone(self.r.tick())          # 同じ予定は1回だけ
        self.at(10, 5)
        self.assertIsNone(self.r.tick())          # 始まったら言わない

    def test_waits_while_away_or_busy(self):
        self.at(9, 55)
        self.idle[0] = 999
        self.assertIsNone(self.r.tick())
        self.idle[0] = 1
        self.a.busy.set()
        self.assertIsNone(self.r.tick())
        self.a.busy.clear()
        self.assertEqual(self.r.tick(), "remind")


class EveningTest(Base):
    def setUp(self):
        super().setUp()
        self.r.state.mark("2026-11-27", "morning")
        self.at(22, 5)

    def test_asks_mood_and_records_answer(self):
        self.assertEqual(self.r.tick(), "evening")
        self.assertEqual(self.s.said[-1], routine.MOOD_Q)
        self.a.handle_text("4かな")
        self.assertIn("- 気分 4/5", self.daily.read_text(encoding="utf-8"))
        self.assertEqual(self.b.calls, [])

    def test_skips_when_mood_already_written(self):
        self.daily.write_text("## メモ\n- 気分 3/5\n", encoding="utf-8")
        self.assertIsNone(self.r.tick())

    def test_answer_too_late_is_a_normal_question(self):
        self.r.tick()
        self.clock[0] += 1000
        self.a.handle_text("4")
        self.assertNotIn("気分", self.daily.read_text(encoding="utf-8"))


class QuietTest(Base):
    def test_quiet_today_then_back(self):
        self.a.handle_text("今日は静かにして")
        self.assertIn("声をかけません", self.s.said[-1])
        self.assertIsNone(self.r.tick())
        self.a.handle_text("声かけして")
        self.assertEqual(self.r.tick(), "morning")


class ParseTest(unittest.TestCase):
    def test_mood(self):
        for text, n in [("4", 4), ("４です", 4), ("三くらい", 3), ("よん", 4), ("5かな", 5), ("わからない", None), ("10", None)]:
            self.assertEqual(routine.parse_mood(text), n, text)

    def test_idle_seconds_reads_ioreg(self):
        class R:
            stdout = '  |   "HIDIdleTime" = 5000000000\n'
        with mock.patch("sys.platform", "darwin"):
            self.assertEqual(routine.idle_seconds(run=lambda *a, **k: R()), 5.0)


if __name__ == "__main__":
    unittest.main()
