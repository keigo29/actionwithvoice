import tempfile
import unittest
from pathlib import Path

from ray import canned, scenes
from ray.app import Assistant
from ray.ui import StateHub
from tests.test_ray import FakeBrain, FakeSpeaker, cfg


class FindTest(unittest.TestCase):
    def test_self_intro_is_builtin(self):
        dirs = [canned.BUILTIN_DIR]
        for q in ["自己紹介して", "紹介してみて", "レイ、自己紹介して。", "あなたは誰？", "君は何者", "何ができるの？", "レイって何ができる？"]:
            self.assertIsNotNone(canned.find(q, dirs), q)
        # 似ているが別の話・定番を直す話・長い話は、いつもの振り分けへ
        for q in ["いいお店を紹介して", "自己紹介を直して", "自己紹介の文を短くして", "友だちに自己紹介するときのコツを教えてほしい"]:
            self.assertIsNone(canned.find(q, dirs), q)

    def test_builtin_intro_has_screens_and_few_sentences(self):
        c = canned.find("自己紹介して", [canned.BUILTIN_DIR])
        parsed = scenes.parse(c.body)
        self.assertEqual([s.kind for s in parsed], ["diagram", "diagram", "steps"])
        self.assertTrue(all(s.params for s in parsed))  # JSON が読めている
        self.assertLessEqual(sum(s.speech.count("。") for s in parsed), 6)

    def test_save_and_find_again(self):
        with tempfile.TemporaryDirectory() as d:
            p = canned.save("今日の筋トレメニューは？", "@画面 steps {\"steps\": [\"ベンチ\"]}\nベンチからです。", Path(d))
            self.assertTrue(p.exists())
            c = canned.find("今日の筋トレメニューは", [d])
            self.assertEqual(c.body, "@画面 steps {\"steps\": [\"ベンチ\"]}\nベンチからです。")
            self.assertIsNone(canned.find("今日の筋トレメニューを教えて", [d]))  # 言い方が同じときだけ

    def test_broken_file_is_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.md").write_text("match: (\n---\n本文", encoding="utf-8")
            (Path(d) / "b.md").write_text("本文だけ", encoding="utf-8")
            self.assertEqual(canned.load([d]), [])

    def test_is_save(self):
        for q in ["今の答えを定番にして", "さっきの返事を定番として保存して", "定番にして"]:
            self.assertTrue(canned.is_save(q), q)
        self.assertFalse(canned.is_save("定番のメニューは？"))


class AssistantCannedTest(unittest.TestCase):
    def make(self, answers):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        b, s, hub = FakeBrain(answers), FakeSpeaker(), StateHub()
        a = Assistant(cfg(cmd_dir=self.tmp.name), b, s, hub, log=lambda *x, **kw: None)
        return a, b, s, hub

    def test_intro_does_not_call_brain(self):
        a, b, s, hub = self.make([])
        a.handle_text("自己紹介して")
        self.assertEqual(b.calls, [])
        self.assertEqual(len(a.deck), 3)  # 図とスライドの場面
        self.assertTrue(any("レイ" in t for t in s.said))

    def test_save_last_answer_then_reuse(self):
        a, b, s, hub = self.make(["@画面 steps {\"steps\": [\"一\"]}\n手順はひとつです。"])
        a.handle_text("片づけの手順は？")
        a.handle_text("今の答えを定番にして")
        self.assertTrue((canned.saved_dir(a.cfg) / "片づけの手順は.md").exists())
        a.handle_text("片づけの手順は？")
        self.assertEqual(len(b.calls), 1)  # 2回目は定番から
        self.assertEqual(s.said.count("手順はひとつです。"), 2)

    def test_save_without_answer(self):
        a, b, s, hub = self.make([])
        a.handle_text("今の答えを定番にして")
        self.assertEqual(s.said[-1], "定番にできる答えが、まだありません")


if __name__ == "__main__":
    unittest.main()
