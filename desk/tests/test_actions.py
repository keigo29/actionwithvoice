import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path

from ray import actions, cmdlog
from ray.app import Assistant
from ray.ui import StateHub
from tests.test_ray import FakeBrain, FakeSpeaker, cfg

JST = dt.timezone(dt.timedelta(hours=9))
NOW = dt.datetime(2026, 11, 27, 9, 30, tzinfo=JST)  # 金曜


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "Inbox.md").write_text("# Inbox\n\n- [ ] 前からある\n", encoding="utf-8")
        (self.root / "Notes").mkdir()
        (self.root / "Notes" / "AIに頼むこと.md").write_text("# AI\n\n## 頼みたいこと\n- [ ] 前の\n\n## 済み\n- [x] 古い\n", encoding="utf-8")
        (self.root / "Projects").mkdir()
        (self.root / "Projects" / "副業.md").write_text("# 副業\n## タスク\n- [ ] 見積もり\n## メモ\nあれこれ\n", encoding="utf-8")
        self.daily = self.root / "2026" / "11" / "27" / "デイリー.md"
        self.daily.parent.mkdir(parents=True)
        self.daily.write_text("# 11/27\n## 予定\n- 19:00 飲み会\n- 10:00-11:00 会議\n## メモ\n- 朝ごはん\n\n## 日記\n", encoding="utf-8")
        tomorrow = self.root / "2026" / "11" / "28" / "デイリー.md"
        tomorrow.parent.mkdir(parents=True)
        tomorrow.write_text("## 予定\n- 12:30 ランチ\n", encoding="utf-8")
        self.c = cfg(cmd_dir=str(self.root))
        self.c["actions"] = {"enabled": True, "push": True}
        self.pushed: list[tuple[Path, str]] = []
        self.t = 0.0
        self.a = actions.Actions(self.c, now=lambda: NOW, clock=lambda: self.t, pull=lambda root: None,
                                 push=lambda c, p, m: self.pushed.append((p, m)))

    def tearDown(self):
        self.tmp.cleanup()

    def text(self, p: Path) -> str:
        return p.read_text(encoding="utf-8")


class TodoTest(Base):
    def test_add_to_inbox_end(self):
        d = self.a.run("牛乳を買うをやることに追加して")
        self.assertEqual(d.text, "インボックスに「牛乳を買う」を足しました。")
        self.assertEqual(self.text(self.root / "Inbox.md"), "# Inbox\n\n- [ ] 前からある\n- [ ] 牛乳を買う\n")
        self.assertEqual(self.pushed[0][0], self.root / "Inbox.md")
        self.assertEqual(d.log["name"], "todo")

    def test_due_date(self):
        d = self.a.run("やることに明日までに資料を送るを追加して")
        self.assertIn("- [ ] 資料を送る 📅 2026-11-28", self.text(self.root / "Inbox.md"))
        self.assertIn("期限は明日です", d.text)

    def test_ai_list_goes_under_its_heading(self):
        self.a.run("AIに頼みたいことに家計のグラフを作るを追加して")
        self.assertEqual(self.text(self.root / "Notes" / "AIに頼むこと.md"),
                         "# AI\n\n## 頼みたいこと\n- [ ] 前の\n- [ ] 家計のグラフを作る\n\n## 済み\n- [x] 古い\n")

    def test_project_tasks(self):
        d = self.a.run("副業のタスクに請求書を出すを追加して")
        self.assertIn("## タスク\n- [ ] 見積もり\n- [ ] 請求書を出す\n## メモ", self.text(self.root / "Projects" / "副業.md"))
        self.assertIn("副業のタスク", d.text)

    def test_already_there_is_not_added_twice(self):
        d = self.a.run("前からあるをやることに追加して")
        self.assertIn("もう", d.text)
        self.assertEqual(self.text(self.root / "Inbox.md").count("前からある"), 1)
        self.assertEqual(self.pushed, [])

    def test_missing_file_goes_to_claude(self):
        (self.root / "Inbox.md").unlink()
        self.assertIsNone(self.a.run("牛乳を買うをやることに追加して"))

    def test_vague_or_other_talk_goes_to_claude(self):
        self.assertIsNone(self.a.run("これをやることに追加して"))
        self.assertIsNone(self.a.run("家計の残りはいくら？"))

    def test_off(self):
        self.c["actions"]["enabled"] = False
        self.assertIsNone(self.a.run("牛乳を買うをやることに追加して"))


class RecordTest(Base):
    def test_weight_into_memo_section(self):
        d = self.a.run("体重70.5キロって記録して")
        self.assertIn("## メモ\n- 朝ごはん\n- 体重 70.5kg\n\n## 日記", self.text(self.daily))
        self.assertEqual(d.text, "体重70.5キロを、今日のデイリーに書きました。")

    def test_sleep_and_mood(self):
        d = self.a.run("4時に寝て7時半に起きた、気分は4って記録して")
        t = self.text(self.daily)
        self.assertIn("- 気分 4/5", t)
        self.assertIn("- 睡眠 4:00-7:30 3.5時間", t)
        self.assertIn("睡眠3.5時間", d.text)

    def test_no_daily_goes_to_claude(self):
        self.daily.unlink()
        self.assertIsNone(self.a.run("体重70キロって記録して"))

    def test_no_memo_heading_goes_to_claude(self):
        self.daily.write_text("# 11/27\n", encoding="utf-8")
        self.assertIsNone(self.a.run("体重70キロって記録して"))


class UndoTest(Base):
    def test_undo_removes_the_line_just_added(self):
        self.a.run("牛乳を買うをやることに追加して")
        d = self.a.run("取り消して")
        self.assertEqual(d.text, "さっき足した行を消しました。")
        self.assertEqual(self.text(self.root / "Inbox.md"), "# Inbox\n\n- [ ] 前からある\n")
        self.assertEqual(len(self.pushed), 2)
        self.assertIsNone(self.a.run("取り消して"))  # 2回目は Ray のものではない：Claude へ

    def test_undo_record_with_several_lines(self):
        before = self.text(self.daily)
        self.a.run("体重70キロ、気分3って記録して")
        self.a.run("今の取り消して")
        self.assertEqual(self.text(self.daily), before)

    def test_too_late(self):
        self.a.run("牛乳を買うをやることに追加して")
        self.t += actions.UNDO_SEC + 1
        self.assertIsNone(self.a.run("取り消して"))


class ScheduleTest(Base):
    def test_today(self):
        d = self.a.run("今日の予定は？")
        self.assertTrue(d.text.startswith("@画面 calendar "))
        self.assertIn("今日の予定は2つです。10時から11時まで、会議。19時から、飲み会です。", d.text)
        self.assertEqual(self.pushed, [])  # 読むだけ

    def test_next(self):
        self.assertIn("次の予定は、今日の10時から11時まで、会議です。", self.a.run("次の予定は？").text)

    def test_week(self):
        d = self.a.run("今週の予定を教えて")
        self.assertIn("今週の予定は3つです。金曜の10時から11時まで、会議。金曜の19時から、飲み会。土曜の12時半から、ランチです。", d.text)

    def test_none(self):
        self.assertIn("予定はありません", self.a.run("11月30日の予定は？").text)

    def test_change_requests_go_to_claude(self):
        self.assertIsNone(self.a.run("明日の予定に歯医者を追加して"))


class InsertTest(unittest.TestCase):
    def test_insert_under_and_remove(self):
        t = "# a\n## x\n- 1\n\n### 小見出し\n- 2\n## y\n"
        self.assertEqual(actions.insert_under(t, "## x", "- new"), "# a\n## x\n- 1\n\n### 小見出し\n- 2\n- new\n## y\n")
        self.assertIsNone(actions.insert_under(t, "## z", "- new"))
        self.assertEqual(actions.insert_under("a\n\n\n", None, "- b"), "a\n- b\n")
        self.assertEqual(actions.remove_last("- a\n- b\n- a\n", "- a"), "- a\n- b\n")

    def test_say_time(self):
        self.assertEqual([actions.say_time(x) for x in ("19:30", "10:00", "09:15")], ["19時半", "10時", "9時15分"])


class AssistantTest(Base):
    def test_brain_is_not_called(self):
        b, s = FakeBrain(["呼ばれないはず"]), FakeSpeaker()
        logs = []
        ast = Assistant(self.c, b, s, StateHub(), log=lambda c, q, a, extra=None: logs.append(extra))
        ast.actions = self.a
        ast.handle_text("牛乳を買うをやることに追加して")
        self.assertEqual(b.calls, [])
        self.assertEqual(s.said, ["インボックスに牛乳を買うを足しました。"])  # 読み上げでは「」を外す
        self.assertEqual(logs[0]["brain"], "ray")
        self.assertEqual(logs[0]["action"]["name"], "todo")
        ast.handle_text("家計の残りはいくら？")  # 決まった頼みごとでなければ、これまでどおり頭脳へ
        self.assertEqual(len(b.calls), 1)


class SyncTest(unittest.TestCase):
    """コミットはすぐ、push はまとめて1回"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        self.remote, self.work = d / "remote.git", d / "work"

        def git(*a, cwd):
            subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True)
        self.git = git
        git("init", "-q", "--bare", "-b", "main", str(self.remote), cwd=d)
        git("clone", "-q", str(self.remote), str(self.work), cwd=d)
        for k, v in (("user.name", "t"), ("user.email", "t@example.com")):
            git("config", k, v, cwd=self.work)
        (self.work / "Inbox.md").write_text("# Inbox\n")
        git("add", ".", cwd=self.work)
        git("commit", "-q", "-m", "init", cwd=self.work)
        git("push", "-q", "-u", "origin", "main", cwd=self.work)

    def tearDown(self):
        self.tmp.cleanup()

    def head(self, where):
        return subprocess.run(["git", "rev-parse", "main"], cwd=where, capture_output=True, text=True).stdout

    def test_batches_and_waits_while_busy(self):
        timers = []

        class T:
            def __init__(self, delay, fn):
                self.delay, self.fn, self.cancelled = delay, fn, False
                timers.append(self)
            def start(self): pass
            def cancel(self): self.cancelled = True

        sync = cmdlog.Syncer(timer=T)
        c = {"cmd_dir": str(self.work), "log": {"push_delay": 60}}
        for i in range(2):
            p = cmdlog.write_qa(self.work, f"q{i}", "a")
            self.assertTrue(sync.save(c, p))
        self.assertEqual(len([t for t in timers if not t.cancelled]), 1)  # 予約は1つだけ
        self.assertNotEqual(self.head(self.remote), self.head(self.work))  # まだ送っていない
        sync.busy = lambda: True
        timers[-1].fn()
        self.assertNotEqual(self.head(self.remote), self.head(self.work))  # 会話の途中は待つ
        sync.busy = lambda: False
        timers[-1].fn()
        self.assertEqual(self.head(self.remote), self.head(self.work))     # 2つのコミットを1回で


if __name__ == "__main__":
    unittest.main()
