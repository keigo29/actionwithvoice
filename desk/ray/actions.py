"""決まった頼みごとを、Claude を呼ばずに Ray が自分で片づける（トークンを使わない）

templates.py が中身まで抜き出せた3つ（やることの追加・体の記録・予定の確認）だけを扱う。
- やることの追加：Inbox.md の末尾／Notes/AIに頼むこと.md の `## 頼みたいこと`／Projects/○○.md の `## タスク` に `- [ ] 中身` を足す
- 体の記録：その日のデイリー（YYYY/MM/DD/デイリー.md）の `## メモ` に足す
- 予定の確認：visuals.collect_events で集めた予定を読み上げ、カレンダーの画面を出す（読むだけ）
書いたら、そのファイルだけをコミットする。push は会話のログと一緒にまとめて（cmdlog.SYNC）。
「取り消して」で、直前に足した1行を消せる。

迷ったら None を返して、これまでどおり Claude に任せる（書く先のファイルや見出しが無い・同じ行がもうある など）。
"""

from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import cmdlog, templates, visuals

UNDO_RE = re.compile(r"^(?:今の|いまの|さっきの)?(?:取り消して|取り消し|とりけして|なしにして|無しにして|やっぱりなし|やっぱり無し)[。.!！]?$")
UNDO_SEC = 600          # 取り消せるのは、足してからこの秒数まで
PULL_TIMEOUT = 8        # 書く前に Cmd を最新にする待ち（ネットが無ければ、そのまま書く）
SPEAK_EVENTS = 5        # 予定を読み上げる数（残りは「ほかに〜つ」）
WEEK = "月火水木金土日"
DEST_NAMES = {"Inbox.md": "インボックス", "Notes/AIに頼むこと.md": "AIに頼みたいこと"}
SECTIONS = {"Notes/AIに頼むこと.md": "## 頼みたいこと"}


@dataclass
class Done:
    text: str                                  # 返事（「@画面」の行を含んでよい）
    log: dict = field(default_factory=dict)    # ログに残す中身（どの作業で、何をしたか）


# ---------------------------------------------------------------- 文の形

def say_time(hm: str) -> str:
    """19:30 → 19時半、10:00 → 10時、10:15 → 10時15分"""
    try:
        h, m = (int(x) for x in hm.split(":"))
    except ValueError:
        return hm
    return f"{h}時" + ("半" if m == 30 else f"{m}分" if m else "")


def say_day(d: dt.date, today: dt.date) -> str:
    diff = (d - today).days
    named = {0: "今日", 1: "明日", 2: "あさって", -1: "昨日"}.get(diff)
    return named or f"{d.month}月{d.day}日（{WEEK[d.weekday()]}）"


def say_event(e: dict) -> str:
    t = e.get("start") or ""
    if not t:
        return e["title"]
    end = e.get("end") or ""
    return f"{say_time(t)}から{say_time(end) + 'まで' if end else ''}、{e['title']}"


# ---------------------------------------------------------------- ファイルに書く

def insert_under(text: str, heading: str | None, line: str) -> str | None:
    """見出しの節の終わり（次の見出しの前）に1行足す。見出しが無ければ None。heading=None ならファイルの末尾"""
    lines = text.split("\n")
    if heading is None:
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines + [line]) + "\n"
    try:
        start = next(i for i, l in enumerate(lines) if l.strip() == heading)
    except StopIteration:
        return None
    level = len(heading) - len(heading.lstrip("#"))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        m = re.match(r"^(#+)\s", lines[i])
        if m and len(m[1]) <= level:
            end = i
            break
    at = end
    while at > start + 1 and lines[at - 1].strip() == "":
        at -= 1  # 節の最後の空行の前に入れる
    lines.insert(at, line)
    return "\n".join(lines)


def remove_last(text: str, line: str) -> str | None:
    lines = text.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        if lines[i] == line:
            del lines[i]
            return "\n".join(lines)
    return None


# ---------------------------------------------------------------- git（書いたファイルだけを同期する）

def git(root: Path, *args: str, timeout: float = 30, run=subprocess.run) -> bool:
    try:
        return run(["git", *args], cwd=str(root), capture_output=True, text=True, timeout=timeout).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def pull_first(root: Path, run=subprocess.run) -> None:
    """書く前に最新にする（iPhone からの追記と同じ所に書いて、ぶつからないように）。だめでも書く"""
    if not (root / ".git").exists():
        return
    with cmdlog._lock:
        if not git(root, "pull", "-q", "--rebase", "--autostash", timeout=PULL_TIMEOUT, run=run):
            git(root, "rebase", "--abort", timeout=10, run=run)  # ぶつかって途中で止まったら、元に戻す


def save(cfg: dict, path: Path, message: str) -> None:
    """そのファイルだけをコミットし、push はまとめて（cmdlog.SYNC）。裏で動かし、会話は待たせない"""
    if not cfg.get("actions", {}).get("push", True) or not (Path(cfg["cmd_dir"]) / ".git").exists():
        return

    def work():
        try:
            if not cmdlog.SYNC.save(cfg, path, message):
                print(f"Cmd にコミットできませんでした（あとで Cmd で push してください）：{path.name}")
        except Exception as e:  # noqa: BLE001 同期の失敗で会話は止めない
            print(f"Cmd を同期できませんでした：{e}")

    threading.Thread(target=work, daemon=True).start()


# ---------------------------------------------------------------- 本体

class Actions:
    def __init__(self, cfg: dict, now: Callable[[], dt.datetime] | None = None, clock=time.monotonic,
                 pull=pull_first, push=save):
        self.cfg = cfg
        self.now = now or (lambda: dt.datetime.now(templates.JST))
        self.clock = clock
        self.pull, self.push = pull, push
        self.last: tuple[Path, str, float] | None = None  # 直前に足した行（取り消し用）

    @property
    def enabled(self) -> bool:
        return self.cfg.get("actions", {}).get("enabled", True)

    def run(self, question: str) -> Done | None:
        """Ray が自分で片づけられたら、その返事。だめなら None（Claude に任せる）"""
        if not self.enabled:
            return None
        if UNDO_RE.match(question.strip()):
            return self.undo()
        from .rewrite import tidy  # 言いよどみ・呼びかけ・言い直しを落とす（テンプレと同じ下ごしらえ）
        t = templates.match(tidy(question), self.cfg["cmd_dir"], self.now())
        if not t:
            return None
        try:
            done = getattr(self, "_" + t.name)({**t.slots, "lines": t.lines})
        except Exception as e:  # noqa: BLE001 Ray が片づけられなければ、Claude に任せる
            print(f"（自分で片づけられなかったので Claude に回します：{e}）")
            return None
        if done:
            done.log = {"name": t.name, "slots": dict(t.slots), **done.log}
        return done

    # --- やることの追加 ---
    def _todo(self, s: dict) -> Done | None:
        root = Path(self.cfg["cmd_dir"])
        dest = s["dest"]
        line = f"- [ ] {s['item']}" + (f" 📅 {s['due']}" if s.get("due") else "")
        heading = SECTIONS.get(dest) or ("## タスク" if dest.startswith("Projects/") else None)
        where = DEST_NAMES.get(dest) or (Path(dest).stem + "のタスク")
        path = root / dest
        if not path.is_file():
            return None  # 書く先が無い：作り方は Claude に任せる
        self.pull(root)
        text = path.read_text(encoding="utf-8")
        if re.search(r"^\s*-\s+\[ \]\s+" + re.escape(s["item"]) + r"(?:\s|$)", text, re.M):
            return Done(f"「{s['item']}」は、もう{where}に入っています。", {"skipped": "already"})
        new = insert_under(text, heading, line)
        if new is None:
            return None
        path.write_text(new, encoding="utf-8")
        self._wrote(path, line, f"Ray：やることを追加 {s['item']}")
        due = dt.date.fromisoformat(s["due"]) if s.get("due") else None
        tail = f"期限は{say_day(due, self.now().date())}です。" if due else ""
        return Done(f"{where}に「{s['item']}」を足しました。{tail}", {"file": dest, "line": line})

    # --- 体の記録 ---
    def _record(self, s: dict) -> Done | None:
        root = Path(self.cfg["cmd_dir"])
        day = dt.date.fromisoformat(s["date"])
        rel = day.strftime("%Y/%m/%d") + "/デイリー.md"
        path = root / rel
        if not path.is_file():
            return None  # デイリーがまだ無い：作るのは Claude に任せる（テンプレの形で作るため）
        self.pull(root)
        text = path.read_text(encoding="utf-8")
        lines = [l for l in s["lines"] if l not in text.split("\n")]
        if not lines:
            return Done("その記録は、もう書いてあります。", {"skipped": "already"})
        new = text
        for line in lines:
            new = insert_under(new, "## メモ", line)
            if new is None:
                return None
        path.write_text(new, encoding="utf-8")
        self._wrote(path, "\n".join(lines), f"Ray：記録 {day.isoformat()}")
        said = []
        if "weight" in s:
            said.append(f"体重{s['weight']}キロ")
        if "mood" in s:
            said.append(f"気分{s['mood']}")
        if "hours" in s:
            said.append(f"睡眠{s['hours']}時間")
        return Done(f"{'、'.join(said)}を、{say_day(day, self.now().date())}のデイリーに書きました。", {"file": rel, "line": lines})

    # --- 予定の確認（読むだけ） ---
    def _schedule(self, s: dict) -> Done:
        root = Path(self.cfg["cmd_dir"])
        now = self.now()
        today = now.date()
        days = [dt.date.fromisoformat(d) for d in s["dates"]]
        span = {d.isoformat() for d in days}
        events = sorted((e for e in visuals.collect_events(root) if e["date"] in span and e["kind"] in ("event", "milestone")),
                        key=lambda e: (e["date"], e.get("start") or "99:99"))
        mode = s.get("mode", "day")
        first = days[0]
        if mode == "next":
            hm = f"{now.hour:02d}:{now.minute:02d}"
            later = [e for e in events if e["date"] > today.isoformat() or (e.get("start") or "") >= hm]
            if not later:
                return Done(self._screen("day", first, []) + "今日と明日は、このあとの予定はありません。", {"events": 0})
            e = later[0]
            d = dt.date.fromisoformat(e["date"])
            return Done(self._screen("day", d, [e]) + f"次の予定は、{say_day(d, today)}の{say_event(e)}です。", {"events": 1})
        if mode == "week":
            label = "今週" if first <= today <= days[-1] else "来週" if 0 < (first - today).days <= 7 else f"{first.month}月{first.day}日からの週"
            if not events:
                return Done(self._screen("week", first, []) + f"{label}の予定はありません。", {"events": 0})
            parts = [f"{WEEK[dt.date.fromisoformat(e['date']).weekday()]}曜の{say_event(e)}" for e in events[:SPEAK_EVENTS]]
        else:
            label = say_day(first, today)
            if not events:
                return Done(self._screen("day", first, []) + f"{label}の予定はありません。", {"events": 0})
            parts = [say_event(e) for e in events[:SPEAK_EVENTS]]
        more = f"ほかに{len(events) - SPEAK_EVENTS}つあります。" if len(events) > SPEAK_EVENTS else ""
        screen = self._screen("week" if mode == "week" else "day", first, events)
        return Done(screen + f"{label}の予定は{len(events)}つです。" + "。".join(parts) + "です。" + more, {"events": len(events)})

    @staticmethod
    def _screen(rng: str, day: dt.date, events: list[dict]) -> str:
        hl = [f"{e['date']} {e['start']}" if e.get("start") else e["title"] for e in events[:SPEAK_EVENTS]]
        return "@画面 calendar " + json.dumps({"range": rng, "date": day.isoformat(), "highlight": hl}, ensure_ascii=False) + "\n"

    # --- 取り消し ---
    def _wrote(self, path: Path, line: str, message: str) -> None:
        self.last = (path, line, self.clock())
        self.push(self.cfg, path, message)

    def undo(self) -> Done | None:
        if not self.last or self.clock() - self.last[2] > UNDO_SEC:
            return None  # Ray が足したものではない：Claude に任せる
        path, block, _ = self.last
        text = path.read_text(encoding="utf-8")
        for line in reversed(block.split("\n")):
            text = remove_last(text, line)
            if text is None:
                self.last = None
                return Done("さっき足した行が見つからなかったので、何も消していません。", {"name": "undo", "skipped": "missing"})
        path.write_text(text, encoding="utf-8")
        self.last = None
        self.push(self.cfg, path, f"Ray：取り消し {path.name}")
        return Done("さっき足した行を消しました。", {"name": "undo", "file": path.name, "line": block})
