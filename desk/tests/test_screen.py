"""画面表示（場面に分ける・絵のデータを作る・見せながら話す）のテスト"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from ray import brain, scenes, visuals
from ray.app import Assistant
from ray.ui import StateHub

from tests.test_ray import FakeBrain, FakeSpeaker, cfg

TODAY = dt.date.today()
D = lambda n=0: (TODAY + dt.timedelta(days=n)).isoformat()  # noqa: E731


def write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def fake_cmd(root: Path) -> None:
    t = TODAY
    write(root, f"{t:%Y/%m/%d}/デイリー.md", f"""---
tags: [日誌]
exercise: 1
weight: 1
---
## 予定
- 09:00-10:00 朝会
- 19:00 飲み会 #節目

## メモ
- 08:12 🎙 今日の予定は？
- 体重 70.4kg
- 気分 4/5 いい感じ
- 気分 2/5
""")
    y = t - dt.timedelta(days=1)
    write(root, f"{y:%Y/%m/%d}/デイリー.md", "---\nexercise: 0\nweight: 1\n---\n## メモ\n- 体重 70.9kg\n")
    write(root, "Inbox.md", f"""# Inbox
- [ ] 牛乳を買う 📅 {D(-1)}
- [ ] 本を返す 📅 {D(2)}
- [x] 洗濯 📅 {D()}
- [ ] 期限なし [[副業]]
""")
    write(root, "Projects/副業.md", f"# 副業\n## タスク\n- [x] 登録する\n- [ ] ポートフォリオ 📅 {D(5)} #節目\n")
    write(root, "Notes/予定メモ.md", f"## {D(1)}\n- 12:30 ランチ\n")
    write(root, "Notes/毎日確認.md", """| 項目 | アイコン | 色 | 曜日 | プロパティ | 休みの日 | 始めた日 |
|---|---|---|---|---|---|---|
| 運動 | dumbbell | #00c230 | 毎日 | exercise | 休む | 2000-01-01 |
| 体重をはかる | activity | #ff5a00 | 毎日 | weight | 続ける | 2000-01-01 |

## 休みの日
""")
    write(root, "Notes/手順.md", "# 手順\n\n1行目\n大事な文はここです\n3行目\n")
    write(root, "Data/kakei.json", json.dumps({"version": 1, "settings": {"budget": 100000}, "accounts": [], "txs": [
        {"type": "expense", "amount": 1200, "date": D(), "category": "food"},
        {"type": "expense", "amount": 800, "date": D(), "category": "food"},
        {"type": "expense", "amount": 5000, "date": D(), "category": "rent"},
        {"type": "income", "amount": 300000, "date": D(), "category": "salary"},
    ]}))
    write(root, "Data/kintore/sets.csv", "date,exercise,kind,weight_kg,e1rm_kg\n"
          f"{D(-7)},ベンチプレス,normal,75,85\n{D()},ベンチプレス,normal,80,90\n{D()},スクワット,warmup,40,45\n")
    write(root, "Templates/デイリー.md", "- [ ] テンプレの中は読まない 📅 2000-01-01\n")


class ScenesTest(unittest.TestCase):
    def test_parse(self):
        text = '調べます\n@画面 calendar {"range": "week"}\n今週は予定が2つ。\n多めです。\n＠画面 todos\nやることです。\n@画面 table {壊れた}\n表です。'
        sc = scenes.parse(text)
        self.assertEqual([s.kind for s in sc], [None, "calendar", "todos", "table"])
        self.assertEqual(sc[0].speech, "調べます")
        self.assertEqual(sc[1].params, {"range": "week"})
        self.assertEqual(sc[1].speech, "今週は予定が2つ。多めです。")  # つなぎ目に空白を入れない
        self.assertEqual(sc[3].params, {})
        self.assertTrue(scenes.has_directive(text))
        self.assertFalse(scenes.has_directive("ふつうの返事です"))
        self.assertEqual(scenes.strip_directives(text), "調べます。今週は予定が2つ。多めです。やることです。表です。")

    def test_prompt_mentions_screen_only_with_ui(self):
        on = brain.build_command(cfg())
        self.assertIn("@画面", " ".join(on))
        off = cfg()
        off["ui"]["enabled"] = False
        self.assertNotIn("@画面", " ".join(brain.build_command(off)))
        self.assertIn("WebSearch", on)
        self.assertIn("diagram", " ".join(on))


class VisualsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        fake_cmd(self.root)
        self.cfg = cfg(cmd_dir=str(self.root))

    def tearDown(self):
        self.tmp.cleanup()

    def b(self, kind, **params):
        return visuals.build(kind, params, self.cfg)

    def test_calendar(self):
        v = self.b("calendar", range="week", highlight=["飲み会"])
        self.assertEqual(v["kind"], "calendar")
        titles = [(e["title"], e["kind"]) for e in v["data"]["events"]]
        self.assertIn(("朝会", "event"), titles)
        self.assertIn(("飲み会", "milestone"), titles)
        self.assertNotIn("今日の予定は？", [t for t, _ in titles])  # 音声ログは予定ではない
        self.assertNotIn("テンプレの中は読まない", [t for t, _ in titles])
        self.assertEqual(len(v["data"]["days"]), 7)
        morning = next(e for e in v["data"]["events"] if e["title"] == "朝会")
        self.assertEqual((morning["start"], morning["end"]), ("09:00", "10:00"))
        day = self.b("calendar", range="day", date=D(1))["data"]
        self.assertEqual([e["title"] for e in day["events"]], ["ランチ"])
        month = self.b("calendar", range="month")["data"]
        self.assertEqual(month["start"][8:], "01")
        self.assertEqual(self.b("予定")["kind"], "calendar")  # 日本語の名前でもよい

    def test_note(self):
        d = self.b("note", path="Notes/手順.md", quote="大事な文")["data"]
        self.assertEqual(d["title"], "手順")
        self.assertEqual([l["n"] for l in d["lines"] if l["hit"]], [4])
        self.assertIn("error", self.b("note", path="../etc/passwd")["data"])

    def test_todos(self):
        d = self.b("todos")["data"]
        names = [i["title"] for i in d["items"]]
        self.assertEqual(names[:2], ["牛乳を買う", "本を返す"])  # 期限の近い順
        self.assertNotIn("洗濯", names)
        self.assertTrue(d["items"][0]["overdue"])
        self.assertIn("期限なし", names)
        prog = {p["name"]: (p["done"], p["total"]) for p in d["projects"]}
        self.assertEqual(prog["副業"], (1, 3))  # Projects の中2つ ＋ [[副業]] の1つ
        only = self.b("todos", project="副業")["data"]
        self.assertEqual({i["title"] for i in only["items"]}, {"ポートフォリオ", "期限なし"})
        soon = self.b("todos", due_within=3)["data"]
        self.assertEqual([i["title"] for i in soon["items"]], ["牛乳を買う", "本を返す"])

    def test_habits(self):
        d = self.b("habits", range="week")["data"]
        self.assertEqual(len(d["days"]), 7)
        ex = next(h for h in d["habits"] if h["name"] == "運動")
        self.assertEqual([c["state"] for c in ex["cells"][-2:]], ["miss", "done"])
        w = next(h for h in d["habits"] if h["name"] == "体重をはかる")
        self.assertEqual(w["streak"], 2)

    def test_body(self):
        d = self.b("body", days=7, target=68)["data"]
        w = next(s for s in d["series"] if s["name"] == "体重")
        self.assertEqual([p["value"] for p in w["points"]], [70.9, 70.4])
        self.assertEqual((w["change"], w["target"]), (-0.5, 68))
        m = next(s for s in d["series"] if s["name"] == "気分")
        self.assertEqual(m["points"][-1]["value"], 3.0)

    def test_kakei(self):
        d = self.b("kakei", labels={"food": "食費"})["data"]
        self.assertEqual((d["expense"], d["income"], d["budget"], d["remaining"]), (7000, 300000, 100000, 93000))
        self.assertEqual(d["categories"][0], {"name": "家賃", "amount": 5000})  # よく使う名前は日本語に
        self.assertEqual(d["categories"][1], {"name": "食費", "amount": 2000})

    def test_workout(self):
        d = self.b("workout")["data"]
        self.assertEqual([s["name"] for s in d["series"]], ["ベンチプレス"])  # ウォームアップは入れない
        self.assertEqual([p["weight"] for p in d["series"][0]["points"]], [75, 80])

    def test_weather(self):
        fake = json.dumps({"hourly": {
            "time": [f"{D()}T{h:02d}:00" for h in range(24)], "temperature_2m": [10 + h / 2 for h in range(24)],
            "precipitation_probability": [h * 3 for h in range(24)], "weather_code": [61] * 24}})
        d = visuals.weather(self.cfg, {"every": 6}, fetch=lambda url: fake)
        self.assertEqual([h["time"] for h in d["hours"]], ["00:00", "06:00", "12:00", "18:00"])
        self.assertEqual((d["min"], d["pop"], d["hours"][0]["sky"]), (10, 69, "小雨"))
        bad = visuals.weather(self.cfg, {}, fetch=lambda url: (_ for _ in ()).throw(OSError("no net")))
        self.assertIn("error", bad)

    def test_diff(self):
        out = "abc123 メモを直す\ndiff --git a/Inbox.md b/Inbox.md\nindex 1..2\n--- a/Inbox.md\n+++ b/Inbox.md\n@@ -1 +1 @@\n-古い\n+新しい\n 同じ\n"

        class R:
            def __init__(self, s): self.stdout = s
        runs = []
        d = visuals.diff(self.root, {}, run=lambda cmd, **k: runs.append(cmd) or R(out if "show" in cmd else "## main\n"))
        self.assertEqual(d["head"], "abc123 メモを直す")
        self.assertEqual([l["kind"] for l in d["files"][0]["lines"]], ["hunk", "del", "add", "ctx"])
        self.assertIn("error", visuals.diff(self.root, {"ref": "HEAD; rm -rf /"}))

    def test_passthrough_and_unknown(self):
        self.assertEqual(self.b("table", columns=["a"], rows=[["1"]])["data"]["rows"], [["1"]])
        self.assertEqual(self.b("比較", columns=[])["kind"], "table")
        dg = self.b("mermaid", code="flowchart LR\n A --> B", highlight=["A"])
        self.assertEqual((dg["kind"], dg["data"]["highlight"]), ("diagram", ["A"]))
        self.assertEqual(self.b("図", code="x")["kind"], "diagram")
        self.assertIn("error", self.b("ufo")["data"])


class PresentTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        fake_cmd(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, answers):
        b, s, hub = FakeBrain(answers), FakeSpeaker(), StateHub()
        shown = []
        orig = hub.set

        def spy(**kw):
            orig(**kw)
            if kw.get("scene"):
                shown.append((kw["scene"]["kind"], kw["scene"]["index"], kw.get("text")))
        hub.set = spy
        a = Assistant(cfg(cmd_dir=self.tmp.name), b, s, hub, log=lambda *x, **kw: None)
        a.shot = lambda url, quote="": None
        return a, b, s, hub, shown

    def test_scenes_are_spoken_in_order_with_pictures(self):
        ans = '@画面 calendar {"range":"week"}\n今週は予定が3つです。\n@画面 calendar {"range":"week","highlight":["飲み会"]}\n今日は飲み会です。'
        a, b, s, hub, shown = self.make([ans])
        out = a.handle_text("今週の予定は？")
        self.assertEqual(s.said, ["今週は予定が3つです。", "今日は飲み会です。"])
        self.assertEqual([x[:2] for x in shown], [("calendar", 0), ("calendar", 1)])
        self.assertEqual(out, "今週は予定が3つです。今日は飲み会です。")  # ログには指示の行を入れない
        self.assertEqual(hub.state["state"], "idle")
        self.assertIsNotNone(hub.state["scene"])  # 話し終えても絵は残す

        # 声での操作（Claude は呼ばない）
        a.handle_text("戻って")
        self.assertEqual(s.said[-1], "今週は予定が3つです。")
        a.handle_text("次")
        self.assertEqual(s.said[-1], "今日は飲み会です。")
        a.handle_text("次")
        self.assertEqual(s.said[-1], "これで最後です")
        a.handle_text("もう一回")
        self.assertEqual(s.said[-2:], ["今週は予定が3つです。", "今日は飲み会です。"])
        a.handle_text("大きく")
        self.assertTrue(hub.state["zoom"])
        a.handle_text("閉じて")
        self.assertIsNone(hub.state["scene"])
        self.assertEqual(len(b.calls), 1)

    def test_plain_answer_and_nav_words_without_deck(self):
        a, b, s, hub, shown = self.make(["こんにちは", "次の話です"])
        a.handle_text("やあ")
        a.handle_text("次")  # 場面が無いときは、ふつうに Claude に聞く
        self.assertEqual(len(b.calls), 2)
        self.assertEqual(shown, [])

    def test_interrupt_stops_scenes(self):
        ans = '@画面 todos\nひとつめ。\n@画面 habits\nふたつめ。'
        a, b, s, hub, shown = self.make([ans])
        orig = s.say
        s.say = lambda t, on_start=None: (orig(t, on_start), a.cut.set())  # 1つ目を話した直後にキーを押した
        a.handle_text("やることは？")
        self.assertEqual(s.said, ["ひとつめ。"])

    def test_sources_get_shots_for_top_pages(self):
        items = [{"title": f"ページ{i}", "url": f"https://example.com/{i}", "site": "example.com"} for i in range(5)]
        ans = '@画面 sources ' + json.dumps({"items": items}, ensure_ascii=False) + '\n調べた結果です。'
        a, b, s, hub, shown = self.make([ans])
        taken = []
        a.shot = lambda url, quote="": taken.append(url) or f"s{len(taken)}.png"
        a.handle_text("近くのラーメン屋を調べて")
        import time
        for _ in range(100):
            got = [it.get("shot") for it in (hub.state.get("scene") or {}).get("data", {}).get("items", [])]
            if got[:3] == ["/shot/s1.png", "/shot/s2.png", "/shot/s3.png"]:
                break
            time.sleep(0.02)
        got = hub.state["scene"]["data"]["items"]
        self.assertEqual([it.get("shot") for it in got], ["/shot/s1.png", "/shot/s2.png", "/shot/s3.png", None, None])
        self.assertEqual(taken, [f"https://example.com/{i}" for i in range(3)])  # 上の3件だけ
        self.assertFalse(any(it.get("shot_pending") for it in got))

    def test_web_shot_added_later_and_confirm_card(self):
        ans = '@画面 web {"url":"https://example.com/a","title":"記事","quote":"根拠の文"}\nこの記事によると上がっています。'
        a, b, s, hub, shown = self.make([ans, "確認です。ファイルを消します。よいですか？"])
        a.shot = lambda url, quote="": "abc.png"
        a.handle_text("円安は？")
        import time
        for _ in range(50):
            if (hub.state.get("scene") or {}).get("data", {}).get("shot"):
                break
            time.sleep(0.02)
        self.assertEqual(hub.state["scene"]["data"]["shot"], "/shot/abc.png")
        self.assertEqual(hub.state["scene"]["data"]["quote"], "根拠の文")
        a.handle_text("消して")
        self.assertEqual(hub.state["state"], "confirm")
        self.assertEqual(hub.state["scene"]["kind"], "confirm")


if __name__ == "__main__":
    unittest.main()


class GuardInPresentTest(PresentTest):
    """読み上げ直前の守り：3文まで・日本語だけ・記号なし"""

    def test_long_plain_answer_stops_at_three_and_continues(self):
        ans = "一つ目です。二つ目です。三つ目です。四つ目です。五つ目です。六つ目です。七つ目です。八つ目です。"
        a, b, s, hub, shown = self.make([ans])
        a.cfg["auto_continue"] = False
        a.handle_text("長い説明して")
        self.assertEqual(s.said, ["一つ目です。二つ目です。三つ目です。四つ目です。五つ目です。六つ目です。",
                                  "続きを聞くときは、続きと言ってください。"])
        a.handle_text("続き")
        self.assertEqual(s.said[-1], "七つ目です。八つ目です。")
        self.assertEqual(len(b.calls), 1)  # 続きは Claude を呼ばない
        self.assertEqual(a.rest, [])  # 話し終えたので、次の「続き」はふつうの質問として Claude に聞く

    def test_auto_continue_plays_everything(self):
        # 既定：「続き」「次」と言わなくても、最後の場面まで話して見せる
        ans = ("@画面 calendar {\"range\":\"week\"}\n月曜です。火曜です。水曜です。\n"
               "@画面 calendar {\"range\":\"week\"}\n木曜です。金曜です。土曜です。日曜です。\n"
               "@画面 calendar {\"range\":\"week\"}\n来週です。")
        a, b, s, hub, shown = self.make([ans])
        a.handle_text("今週の予定")
        self.assertEqual(s.said, ["月曜です。火曜です。水曜です。", "木曜です。金曜です。土曜です。日曜です。", "来週です。"])
        self.assertEqual(a.pos, 2)
        self.assertEqual(a.rest, [])

    def test_new_question_forgets_rest(self):
        a, b, s, hub, shown = self.make(["一。二。三。四。", "はい。"])
        a.handle_text("長く")
        a.handle_text("別の質問")
        self.assertEqual(a.rest, [])

    def test_scenes_stop_at_three_sentences(self):
        ans = ("@画面 calendar {\"range\":\"week\"}\n月曜です。火曜です。水曜です。\n"
               "@画面 calendar {\"range\":\"week\"}\n木曜です。金曜です。土曜です。日曜です。\n"
               "@画面 calendar {\"range\":\"week\"}\n来週です。")
        a, b, s, hub, shown = self.make([ans])
        a.cfg["auto_continue"] = False
        a.handle_text("今週の予定")
        self.assertEqual(s.said, ["月曜です。火曜です。水曜です。", "木曜です。金曜です。土曜です。", "続きは、次と言ってください。"])
        self.assertEqual(a.pos, 1)
        a.handle_text("次")
        self.assertEqual(s.said[-1], "来週です。")

    def test_english_and_symbols_are_spoken_in_japanese(self):
        ans = "中身は「Claude」で、Cmdのkakei.jsonを読みます！上半身Aは18:45からです。"
        a, b, s, hub, shown = self.make([ans])
        a.handle_text("教えて")
        self.assertEqual(s.said, ["中身はクロードで、コマンドのファイルを読みます。上半身エーは18時45分からです。"])

    def test_log_keeps_raw_and_trimmed(self):
        logs = []
        ans = '@画面 todos {}\nやることは7つです。一つ目。二つ目。三つ目。四つ目。五つ目。六つ目。'
        a, b, s, hub, shown = self.make([ans])
        a.log = lambda c, q, answer, extra=None: logs.append((answer, extra))
        a.cfg["auto_continue"] = False
        a.handle_text("やること")
        answer, extra = logs[0]
        self.assertEqual(answer, "やることは7つです。一つ目。二つ目。三つ目。四つ目。五つ目。六つ目。")
        self.assertEqual(extra["raw"], ans)  # 画面の指示の行も残す
        self.assertTrue(extra["trimmed"])
