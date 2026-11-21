"""依頼のテンプレ（やることの追加・記録・予定の確認）"""

import unittest

from ray import rewrite, templates
from tests.test_rewrite import NOW, Base, FakeOllama


def match(text, root):
    return templates.match(rewrite.tidy(text), root, NOW)


class TodoTest(Base):
    def test_inbox_by_default(self):
        t = match("やることリストにUIの改善とアイコンの改善を送っておいて", self.root)
        self.assertEqual(t.name, "todo")
        self.assertEqual(t.slots, {"dest": "Inbox.md", "item": "UIの改善とアイコンの改善", "due": ""})
        self.assertIn("書き方：`- [ ] 中身`", t.prompt)
        self.assertIn("中身が2つ以上なら1行ずつ", t.prompt)
        self.assertIn("git push", t.prompt)
        self.assertTrue(t.prompt.endswith("持ち主の言葉：やることリストにUIの改善とアイコンの改善を送っておいて"))

    def test_due_date(self):
        t = match("えーっと、来週の水曜までに書類を出すをやることに追加して", self.root)
        self.assertEqual(t.slots, {"dest": "Inbox.md", "item": "書類を出す", "due": "2026-11-25"})
        self.assertIn("` 📅 2026-11-25`", t.prompt)

    def test_project_only_when_named_before_tasks(self):
        self.assertEqual(match("副業のタスクに案件を3件探すを追加して", self.root).slots["dest"], "Projects/副業.md")
        self.assertIn("`## タスク` の下", match("副業のタスクに案件を3件探すを追加して", self.root).prompt)
        self.assertEqual(match("副業の案件を探すをやることに追加して", self.root).slots["dest"], "Inbox.md")

    def test_ai_requests_go_to_their_note(self):
        t = match("AIに頼みたいことにログの集計を入れておいて", self.root)
        self.assertEqual(t.slots["dest"], "Notes/AIに頼むこと.md")
        self.assertIn("`## 頼みたいこと` の下", t.prompt)

    def test_vague_item_is_left_to_7b(self):
        self.assertIsNone(match("これをやることに追加して", self.root))


class RecordTest(Base):
    def test_sleep_from_bed_and_wake(self):
        q = "会社行くのめんどくせえよ俺の睡眠時間何時間だろ今日はたぶん4時くらい寝て7時半に起きちゃったそう記録しといて"
        t = match(q, self.root)
        self.assertEqual(t.name, "record")
        self.assertIn("2026/11/19/デイリー.md の `## メモ` の下", t.prompt)
        self.assertIn("- 睡眠 4:00-7:30 3.5時間", t.prompt)
        self.assertIn("睡眠時間を聞かれていたら", t.prompt)

    def test_sleep_across_midnight(self):
        self.assertEqual(match("23時半に寝て6時に起きたって記録して", self.root).slots["hours"], "6.5")

    def test_weight_and_mood(self):
        t = match("体重70.5キロと気分4を記録して", self.root)
        self.assertIn("- 体重 70.5kg\n- 気分 4/5", t.prompt)

    def test_full_width_and_other_day(self):
        t = match("昨日の体重は７１キロだった、つけておいて", self.root)
        self.assertEqual(t.slots, {"weight": "71", "date": "2026-11-18"})
        self.assertIn("2026/11/18/デイリー.md", t.prompt)

    def test_nothing_to_write_is_left_to_7b(self):
        self.assertIsNone(match("今日の気分は最悪って記録して", self.root))
        self.assertIsNone(match("体重どうなってる？", self.root))  # 記録の頼みではない


class ScheduleTest(Base):
    def test_next(self):
        t = match("そういえば次の予定何か把握されてる?", self.root)
        self.assertEqual(t.name, "schedule")
        self.assertEqual(t.slots["dates"], ["2026-11-19", "2026-11-20"])
        self.assertIn("今（12:00）より後で、いちばん近い予定", t.prompt)
        self.assertIn("読むだけ。ファイルは変えない", t.prompt)

    def test_said_day_and_week(self):
        self.assertEqual(match("来週の水曜の予定は？", self.root).slots["dates"], ["2026-11-25"])
        week = match("今週の予定教えて", self.root).slots["dates"]
        self.assertEqual((week[0], week[-1], len(week)), ("2026-11-16", "2026-11-22", 7))
        self.assertEqual(match("今日の予定は？", self.root).slots["dates"], ["2026-11-19"])

    def test_edits_are_not_schedule_checks(self):
        self.assertIsNone(match("明日の予定に歯医者を追加して", self.root))
        self.assertIsNone(match("金曜の予定を消して", self.root))

    def test_other_questions(self):
        self.assertIsNone(match("React.jsを詳しく説明して", self.root))


class FlowTest(Base):
    def test_template_skips_7b_even_when_short(self):
        o = FakeOllama()
        h, claude = self.hybrid(o)
        res = h.ask("今日の予定は？", lambda t: None)
        self.assertEqual(o.bodies, [])  # 7B は呼ばない
        self.assertIn("作業：予定を答える", claude.calls[0]["text"])
        log = res.rewrite
        self.assertEqual((log["applied"], log["reason"], log["template"]), (True, "template", "schedule"))
        self.assertEqual(log["after"], claude.calls[0]["text"])
        self.assertEqual(log["missing"], [])

    def test_template_works_without_local_llm(self):
        o = FakeOllama(loaded=False)
        h, claude = self.hybrid(o)
        res = h.ask("体重70.5キロを記録して", lambda t: None)
        self.assertEqual(res.rewrite["template"], "record")

    def test_long_history_keeps_session(self):
        o = FakeOllama()
        h, claude = self.hybrid(o)
        sid = None
        for i in range(rewrite.MIN_HISTORY_TURNS):
            sid = h.ask(f"フォルダ{i}は？", lambda t: None, session_id=sid).session_id
        res = h.ask("次の予定は何かある？", lambda t: None, session_id=sid)
        self.assertEqual(claude.calls[-1]["session_id"], f"claude-{len(claude.calls) - 1}")
        self.assertFalse(res.rewrite["fresh_session"])
        self.assertEqual(o.bodies, [])

    def test_can_turn_off(self):
        self.cfg["rewrite"] = {"templates": False, "refs_always": False}
        h, claude = self.hybrid(FakeOllama())
        h.ask("今日の予定は？", lambda t: None)
        self.assertEqual(claude.calls[0]["text"], "今日の予定は？")
        self.cfg["rewrite"] = {"templates_on_short": False, "refs_always": False}
        h, claude = self.hybrid(FakeOllama())
        h.ask("今日の予定は？", lambda t: None)
        self.assertEqual(claude.calls[0]["text"], "今日の予定は？")

    def test_no_template_falls_back_to_7b_flow(self):
        o = FakeOllama(outputs=[{"依頼": "ダッシュボードの改善案を3つ考えて答える", "要約": ""}])
        h, claude = self.hybrid(o)
        q = "えっとさ、このダッシュボードの改善案を3つくらい考えてほしいんだけど、見た目と速さの両方で、どうかな、思いつくものある？"
        res = h.ask(q, lambda t: None)
        self.assertEqual(len(o.bodies), 1)
        self.assertNotIn("template", res.rewrite)
        self.assertIn("依頼：ダッシュボードの改善案を3つ考えて答える", claude.calls[0]["text"])


if __name__ == "__main__":
    unittest.main()
