"""依頼のテンプレ：決まった形でくり返す頼みごとは、コードで中身を抜き出して、決まった依頼にする

プロンプト変換（rewrite.py）の最初の段。当たったら 7B は呼ばない（速い・数字が落ちない・毎回同じ形）。
テンプレは「やることの追加」「記録（睡眠・気分・体重）」「予定の確認」の3つだけ（Data/ray のログに何度も出たもの）。
間違ったテンプレは言い換えより悪いので、言葉ではっきり当たり、中身まで抜き出せたときだけ使う。
迷ったら None を返し、これまでの流れ（7B の言い換え）に任せる。
依頼には持ち主の言葉（下ごしらえ済み）も必ず添え、抜き出しの取りこぼしを Claude が補えるようにする。
書き方の決まりは Cmd の CLAUDE.md・README.md のとおり。
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

HEADER = "（Ray が決まった形にした依頼）"
SYNC = "Cmd を変えたら git pull --rebase してからコミットし、git push する。"


@dataclass
class Template:
    name: str                       # todo / record / schedule
    prompt: str                     # Claude に渡す依頼
    slots: dict = field(default_factory=dict)   # 抜き出した中身（ログ用）


def _n(text: str) -> str:
    return unicodedata.normalize("NFKC", text)


def _day(d: date) -> str:
    return f"{d.isoformat()}（{WEEKDAYS[d.weekday()]}）"


def _daily(d: date) -> str:
    return d.strftime("%Y/%m/%d") + "/デイリー.md"


def _dates(text: str, now: datetime) -> list[date]:
    from .local import said_dates  # local が rewrite を読み込むので、ここで遅れて読む
    return [date.fromisoformat(d) for d in said_dates(text, now)]


def _projects(text: str, root: Path) -> list[str]:
    d = root / "Projects"
    if not d.is_dir():
        return []
    return [f.stem for f in sorted(d.glob("*.md"), key=lambda f: -len(f.stem))
            if len(f.stem) >= 2 and f.stem.lower() in text.lower()]


# ---------------------------------------------------------------- 1. やることの追加

_DEST = r"(?:AIに頼(?:みたい|む)こと(?:リスト)?|頼みたいこと(?:リスト)?|やること(?:リスト)?|ToDo(?:リスト)?|タスク(?:リスト)?|インボックス|inbox)"
_ADD = r"(?:追加|入れ|足し|足す|送っ|送る|書い|書き|書く|登録|積ん)"
_TODO_A = re.compile(_DEST + r"(?:に|へ)[、\s]*(.+?)(?:を|って)[、\s]*" + _ADD, re.I)
_TODO_B = re.compile(r"(?:^|[、。\s])([^、。]+?)を[、\s]*" + _DEST + r"(?:に|へ)[、\s]*" + _ADD, re.I)
_VAGUE = re.compile(r"^(?:これ|それ|あれ|さっきの|今の|その|この)(?:こと|やつ|話|件)?$")
_DUE = re.compile(r"まで|期限|締め?切")


def todo(text: str, root: Path, now: datetime) -> Template | None:
    m = _TODO_A.search(text) or _TODO_B.search(text)
    if not m:
        return None
    item = m.group(1).strip("、。 ")
    item = re.sub(r"^(?:この|あの)?(?:リスト|一覧)(?:に|へ)", "", item)
    if len(item) < 2 or _VAGUE.match(item):
        return None  # 「これを追加して」は、何のことかを 7B に解かせる
    dest_word = re.search(_DEST, text, re.I).group(0)
    if "頼" in dest_word:
        dest, where = "Notes/AIに頼むこと.md", "`## 頼みたいこと` の下（プロジェクトにはしない。WBS に出さないため）"
    elif (projects := [p for p in _projects(text, root) if re.search(re.escape(p) + r"の?" + _DEST, text, re.I)]):
        dest, where = f"Projects/{projects[0]}.md", "`## タスク` の下"  # 「副業のタスクに〜」のときだけ
    else:
        dest, where = "Inbox.md", "末尾（行き先の決まっていないやること）"
    due = ""
    days = _dates(text, now)
    if days and _DUE.search(text):
        due = days[0].isoformat()
        item = re.sub(r"^.*?(?:まで(?:に)?|期限|締め?切り?)[はが、\s]*", "", item).strip("、。 ") or item
    lines = [
        HEADER,
        "作業：やることを足す",
        f"足す先：{dest} の{where}",
        "書き方：`- [ ] 中身`" + (f"、行の末尾に ` 📅 {due}`" if due else "") + "。中身が2つ以上なら1行ずつ。ほかの行は変えない",
        f"中身：{item}",
    ]
    if due:
        lines.append(f"期限：{_day(date.fromisoformat(due))}")
    lines += [f"読むのは足す先のファイルだけ。{SYNC}", "終わったら、足したことを短く伝える。"]
    return Template("todo", "\n".join(lines), {"dest": dest, "item": item, "due": due})


# ---------------------------------------------------------------- 2. 記録（睡眠・気分・体重）

_RECORD = re.compile(r"記録|つけ(?:て|と)|メモ(?:し|っ)|書い(?:て|と)|残し|入れ(?:て|と)|足し(?:て|と)")
_WEIGHT = re.compile(r"体重[はがを]?[、\s]*(\d{2,3}(?:\.\d+)?)\s*(?:kg|キロ)?", re.I)
_MOOD = re.compile(r"気分[はがを]?[、\s]*([1-5])(?:\s*/\s*5|点)?(?!\d)")
_CLOCK = r"(\d{1,2})時(?:(半)|(\d{1,2})分)?(?:くらい|ぐらい|ごろ|頃)?に?"
_BED = re.compile(_CLOCK + r"(?:寝|眠)")
_WAKE = re.compile(_CLOCK + r"(?:起き|目が覚め|目覚め)")
_HOURS = re.compile(r"(\d{1,2}(?:\.\d+)?)時間(?:寝|眠|睡眠)|睡眠(?:時間)?[はが]?[、\s]*(\d{1,2}(?:\.\d+)?)時間")


def _clock(m: re.Match) -> tuple[int, int]:
    return int(m.group(1)) % 24, 30 if m.group(2) else int(m.group(3) or 0)


def record(text: str, root: Path, now: datetime) -> Template | None:
    t = _n(text)
    if not _RECORD.search(t):
        return None
    entries, slots = [], {}
    if (m := _WEIGHT.search(t)):
        slots["weight"] = m.group(1)
        entries.append(f"- 体重 {m.group(1)}kg")
    if (m := _MOOD.search(t)):
        slots["mood"] = int(m.group(1))
        entries.append(f"- 気分 {m.group(1)}/5")
    bed, wake, hours = _BED.search(t), _WAKE.search(t), _HOURS.search(t)
    if bed and wake:
        (bh, bm), (wh, wm) = _clock(bed), _clock(wake)
        mins = ((wh * 60 + wm) - (bh * 60 + bm)) % (24 * 60)
        h = f"{mins / 60:.1f}".rstrip("0").rstrip(".")
        slots.update(bed=f"{bh}:{bm:02d}", wake=f"{wh}:{wm:02d}", hours=h)
        entries.append(f"- 睡眠 {bh}:{bm:02d}-{wh}:{wm:02d} {h}時間")
    elif hours:
        h = hours.group(1) or hours.group(2)
        slots["hours"] = h
        entries.append(f"- 睡眠 {h}時間")
    if not entries:
        return None  # 何を書くか抜き出せないものは、テンプレにしない
    days = _dates(t, now)
    day = days[0] if days else now.date()
    slots["date"] = day.isoformat()
    lines = [
        HEADER,
        "作業：体の記録を書く",
        f"書く先：{_daily(day)} の `## メモ` の下（ファイルが無ければ Templates/デイリー.md の形で作る）",
        "書く行（このまま。前の行は消さない）：",
        *entries,
        "気分は `- 気分 4/5 ひとこと`、体重は `- 体重 70.5kg` の形（README.md の決まり）。睡眠は起きた日のデイリーに書く",
        f"読むのは書く先のファイルだけ。{SYNC}",
        "終わったら、書いた内容を短く伝える（睡眠時間を聞かれていたら、その時間も言う）。",
    ]
    return Template("record", "\n".join(lines), slots)


# ---------------------------------------------------------------- 3. 予定の確認

_SCHED = re.compile(r"予定|スケジュール|カレンダー")
_EDIT = re.compile(r"追加|入れ|足し|変え|消し|削除|移し|ずらし|書い|書き|登録|キャンセル|直し|記録")
_ASK = re.compile(r"何|ある|教え|把握|確認|空い|いつ|どう|[？?]|知りたい|見せ|読ん|入って")
_NEXT = re.compile(r"次の|このあと|この後|これから")
_WEEK = re.compile(r"(今週|来週|再来週)(?![のは]?[月火水木金土日]曜)")


def schedule(text: str, root: Path, now: datetime) -> Template | None:
    t = _n(text)
    if not (_SCHED.search(t) and _ASK.search(t)) or _EDIT.search(t):
        return None
    today = now.date()
    days = _dates(t, now)
    want = "その日の予定を、時刻の順に答える"
    if (w := _WEEK.search(t)) and not days:
        start = today - timedelta(days=today.weekday()) + timedelta(weeks={"今週": 0, "来週": 1, "再来週": 2}[w.group(1)])
        days = [start + timedelta(days=i) for i in range(7)]
        want = "その週の予定を、日にちの順に答える"
    elif _NEXT.search(t) and not days:
        days = [today, today + timedelta(days=1)]
        want = f"今（{now.hour}:{now.minute:02d}）より後で、いちばん近い予定を答える"
    elif not days:
        days = [today]
    days = days[:7]
    heads = "、".join(f"## {d.isoformat()}" for d in days)
    lines = [
        HEADER,
        "作業：予定を答える（読むだけ。ファイルは変えない）",
        f"今：{_day(today)} {now.hour}:{now.minute:02d}",
        "対象の日：" + "、".join(_day(d) for d in days),
        "読むファイル：" + "、".join(_daily(d) for d in days) + "（無いファイルは、その日のデイリーには予定なし）",
        f"ほかのノートは grep で見出し（{heads}）のある所だけ探す。全部は読まない",
        "予定の形：`- HH:MM 内容` か `- HH:MM-HH:MM 内容`（デイリーの `## 予定` の下か、日付の見出しの下）",
        f"答えること：{want}。予定が無ければ、無いと短く言う",
    ]
    return Template("schedule", "\n".join(lines), {"dates": [d.isoformat() for d in days]})


RULES = (todo, record, schedule)  # 先にあるほど優先（「予定をやることに追加」は todo）


def match(text: str, cmd_dir: str | Path, now: datetime | None = None) -> Template | None:
    """当たったテンプレの依頼（持ち主の言葉を最後に添える）。当たらなければ None"""
    n = (now or datetime.now(JST)).astimezone(JST)
    root = Path(cmd_dir).expanduser()
    for rule in RULES:
        try:
            t = rule(text, root, n)
        except Exception:  # テンプレの失敗で会話は止めない（これまでの流れに任せる）
            t = None
        if t:
            t.prompt += f"\n持ち主の言葉：{text}"
            return t
    return None
