"""Ray から声をかける（朝のまとめ・予定の前の知らせ・夜のふりかえり）。どれも Claude を呼ばない

- 朝：その日はじめて机にいると分かったとき（決まった時間帯の中で）、天気 → 今日の予定 → 期限の近いやること を話す。
  ゆうべの睡眠がデイリーに無ければ「何時に寝て、何時に起きましたか？」と聞き、答えを記録する
- 予定の前：今日の予定の少し前（既定10分）に知らせる
- 夜：決まった時刻に「今日の気分は5段階でいくつですか？」と聞き、答えを記録する

うるさくしないための決まり
- 机にいないとき（Mac をしばらく触っていないとき）は話さない。戻ってきたら話す（予定の知らせは、始まったら捨てる）
- 会話の途中・話している途中は待つ
- 「今日は静かに」で、その日は声をかけない（「声かけして」で戻る）
- 同じ日に同じ声かけは1回だけ（Ray を起動し直しても、routine-state.json に覚えておく）
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import sys
import threading
import time
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Callable

from . import templates, visuals
from .actions import say_day, say_event

STATE_FILE = Path(__file__).resolve().parent.parent / "routine-state.json"
TICK_SEC = 20
EVENTS_SEC = 300  # 予定を集め直す間隔（Cmd の全部の .md を読むので、毎回はしない）

QUIET_RE = re.compile(r"^(?:今日は)?(?:静かに|しずかに)(?:して)?(?:ください)?[。.!！]?$|^(?:今日は)?声かけ(?:は|を)?(?:しないで|いらない|やめて)[。.!！]?$")
LOUD_RE = re.compile(r"^声かけ(?:を)?(?:して|再開|戻して)[。.!！]?$")
MORNING_RE = re.compile(r"^(?:おはよう(?:ございます)?|朝のまとめ(?:を)?(?:お願い|して|教えて)?)[。.!！]?$")

MOOD_Q = "今日もおつかれさまでした。今日の気分は、5段階でいくつですか？"
SLEEP_Q = "ゆうべは何時に寝て、何時に起きましたか？"
KANJI = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
KANA = {"いち": 1, "に": 2, "さん": 3, "よん": 4, "し": 4, "ご": 5}


def hm(s: str) -> dt.time:
    h, m = (int(x) for x in str(s).split(":"))
    return dt.time(h % 24, m)


def idle_seconds(run=subprocess.run) -> float | None:
    """Mac をさわっていない秒数（キーボード・マウス）。分からなければ None（机にいるとみなす）"""
    if sys.platform != "darwin":
        return None
    try:
        out = run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out)
    return int(m[1]) / 1e9 if m else None


# ---------------------------------------------------------------- 答えを読む

def parse_mood(text: str) -> int | None:
    t = unicodedata.normalize("NFKC", text).strip()
    m = re.search(r"(?<!\d)([1-5])(?!\d)", t)
    if m:
        return int(m[1])
    for k, v in KANJI.items():
        if k in t:
            return v
    bare = re.sub(r"[。、！？!?\s]|かな$|です$|くらい$|ぐらい$", "", t)
    return KANA.get(bare)


def mood_phrase(text: str) -> str | None:
    n = parse_mood(text)
    return f"気分は{n}って記録して" if n else None


def sleep_phrase(text: str, cmd_dir: str, now: dt.datetime) -> str | None:
    """「1時に寝て7時に起きた」→ 記録の頼み（テンプレで睡眠が抜き出せるときだけ）"""
    phrase = f"{text.strip().rstrip('。')}って記録して"
    t = templates.match(phrase, cmd_dir, now)
    return phrase if t and t.name == "record" and "hours" in t.slots else None


# ---------------------------------------------------------------- 朝のまとめ（コードだけで作る）

def daily_text(root: Path, day: dt.date) -> str | None:
    p = root / day.strftime("%Y/%m/%d") / "デイリー.md"
    return p.read_text(encoding="utf-8") if p.is_file() else None


def weather_part(cfg: dict, today: dt.date) -> str:
    w = visuals.weather(cfg, {"date": today.isoformat(), "every": 1})
    if w.get("error"):
        return "おはようございます。"
    day = [h for h in w["hours"] if "06:00" <= h["time"] <= "21:00"] or w["hours"]
    sky = Counter(h["sky"] for h in day if h["sky"]).most_common(1)
    pop = max((h["pop"] for h in day), default=0)
    s = f"おはようございます。今日の{w['place']}は{sky[0][0] + '、' if sky else ''}最高{w['max']}度、最低{w['min']}度です。"
    if pop >= 30:
        s += f"雨の確率は{pop}パーセントです。" + ("傘を持っていくと安心です。" if pop >= 50 else "")
    return "@画面 weather " + json.dumps({"date": today.isoformat()}) + "\n" + s


def todos_part(root: Path, within: int = 3) -> str:
    d = visuals.todos(root, {"due_within": within})
    items = d["items"]
    if not items:
        s = f"期限が{within}日以内のやることはありません。"
    else:
        late = sum(1 for i in items if i.get("overdue"))
        s = f"期限が{within}日以内のやることは{len(items)}つです。"
        if late:
            s += f"そのうち{late}つは、期限を過ぎています。"
        s += f"いちばん近いのは、{items[0]['title']}です。"
    return "@画面 todos " + json.dumps({"due_within": within}) + "\n" + s


def brief(cfg: dict, actions, now: dt.datetime) -> tuple[str, str | None]:
    """朝のまとめ（返事と同じ形の文）と、続けて聞くこと（sleep / None）"""
    root, today = Path(cfg["cmd_dir"]), now.date()
    parts = [weather_part(cfg, today), actions._schedule({"dates": [today.isoformat()], "mode": "day"}).text.strip(),
             todos_part(root)]
    ask = None
    daily = daily_text(root, today)
    if cfg.get("routine", {}).get("sleep_question", True) and daily is not None and not re.search(r"^\s*-\s+睡眠", daily, re.M):
        parts[-1] += SLEEP_Q
        ask = "sleep"
    return "\n".join(parts), ask


# ---------------------------------------------------------------- その日にしたこと（起動し直しても覚えておく）

class State:
    def __init__(self, path: Path = STATE_FILE):
        self.path = path
        self.lock = threading.Lock()
        try:
            self.d = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.d = {}

    def _day(self, day: str) -> dict:
        if self.d.get("date") != day:
            self.d = {"date": day, "done": [], "quiet": False}
        return self.d

    def done(self, day: str, key: str) -> bool:
        with self.lock:
            return key in self._day(day)["done"]

    def mark(self, day: str, key: str) -> None:
        with self.lock:
            self._day(day)["done"].append(key)
            self._save()

    def quiet(self, day: str) -> bool:
        with self.lock:
            return bool(self._day(day).get("quiet"))

    def set_quiet(self, day: str, on: bool) -> None:
        with self.lock:
            self._day(day)["quiet"] = on
            self._save()

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(self.d, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass


# ---------------------------------------------------------------- 声をかける係

class Routine:
    def __init__(self, cfg: dict, assistant, now: Callable[[], dt.datetime] | None = None,
                 idle: Callable[[], float | None] = idle_seconds, state: State | None = None, clock=time.monotonic):
        self.cfg, self.a = cfg, assistant
        self.c = cfg.get("routine", {})
        self.now = now or (lambda: dt.datetime.now(templates.JST))
        self.idle = idle
        self.state = state or State()
        self.clock = clock
        self.events: tuple[float, list[dict]] = (-1e9, [])
        self.stop_flag = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        while not self.stop_flag.wait(TICK_SEC):
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001 声かけの失敗で、Ray は止めない
                print(f"声かけのエラー：{e}")

    def present(self) -> bool:
        sec = self.idle()
        return sec is None or sec < float(self.c.get("present_sec", 180))

    def tick(self) -> str | None:
        """今、声をかけるべきなら声をかける。かけたものの名前を返す（テスト用）"""
        if not self.c.get("enabled", True):
            return None
        now = self.now()
        day = now.date().isoformat()
        if self.state.quiet(day) or not self.a.idle() or not self.present():
            return None
        t = now.time()
        m = self.c.get("morning", {})
        if m.get("enabled", True) and not self.state.done(day, "morning") and hm(m.get("from", "05:00")) <= t < hm(m.get("until", "11:00")):
            self.state.mark(day, "morning")
            self.a.morning_brief(now)
            return "morning"
        r = self.c.get("remind", {})
        if r.get("enabled", True):
            e = self._next_event(now, int(r.get("lead_min", 10)))
            if e:
                key = f"remind {e['date']} {e['start']} {e['title']}"
                if not self.state.done(day, key):
                    self.state.mark(day, key)
                    mins = max(1, round((self._at(e) - now).total_seconds() / 60))
                    screen = "@画面 calendar " + json.dumps({"range": "day", "date": e["date"], "highlight": [f"{e['date']} {e['start']}"]}, ensure_ascii=False)
                    self.a.announce(f"{screen}\nあと{mins}分で、{say_event(e)}です。", title="予定の知らせ", source="ray")
                    return "remind"
        ev = self.c.get("evening", {})
        if ev.get("enabled", True) and not self.state.done(day, "evening") and hm(ev.get("at", "22:00")) <= t < hm(ev.get("until", "23:59")):
            self.state.mark(day, "evening")
            daily = daily_text(Path(self.cfg["cmd_dir"]), now.date())
            if daily is not None and re.search(r"^\s*-\s+気分", daily, re.M):
                return None  # もう書いてある
            self.a.ask_back(MOOD_Q, "mood", title="夜のふりかえり")
            return "evening"
        return None

    @staticmethod
    def _at(e: dict) -> dt.datetime:
        return dt.datetime.fromisoformat(f"{e['date']}T{e['start']}").replace(tzinfo=templates.JST)

    def _next_event(self, now: dt.datetime, lead: int) -> dict | None:
        at, events = self.events
        if self.clock() - at > EVENTS_SEC:
            today = now.date().isoformat()
            events = [e for e in visuals.collect_events(Path(self.cfg["cmd_dir"]))
                      if e["date"] == today and e.get("start") and e["kind"] in ("event", "milestone")]
            self.events = (self.clock(), events)
        for e in sorted(events, key=lambda e: e["start"]):
            left = (self._at(e) - now).total_seconds()
            if 0 < left <= lead * 60:
                return e
        return None
