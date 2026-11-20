"""振り分け：ローカル LLM（Ollama の 7B 級）→ Gemini 無料枠 → Claude

ノート（Cmd の Notes/音声連携.md「振り分けとフォールバック」「設計の決定事項」）の方針：
- 一次受けはローカル。あいさつ・一般知識・短い返事は、外に出さずにその場で答える
- ローカルは Cmd を自分では読めないので、体調・健康の記録（体重・気分・習慣・筋トレ）は Ray が読んで渡す
  ローカルで答えられなければ Gemini に渡す（決めた線引きで、体調・健康の記録は Gemini 無料枠でよい側）
- ローカルで足りない軽い質問は Gemini の無料枠（Flash-Lite）。Cmd のデータは渡さない
- お金の詳細・仕事の機密・予定やメモ・作業・調べものは Claude。迷うものも Claude に倒す
- 調べもの（ウェブ・最新の情報）は、持ち主のデータを含まなければ Gemini に Google 検索で答えさせる
- 振り分けは、明らかなものは言葉のルール、迷うものはモデルに決めさせる（「@クロード」と返させる）
- Gemini の無料枠の上限（429）・鍵がない・止まっているときは Claude へ回す。有料枠は使わない
"""

from __future__ import annotations

import csv
import json
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import visuals
from .brain import Result
from .rewrite import Rewriter, tidy
from .search import SearchBrain

JST = timezone(timedelta(hours=9))
HANDOFF = "@クロード"
WEEKDAYS = "月火水木金土日"

SPEAK_RULES = "話し方：日本語だけ。全部で3文まで。1文は40字くらいまで。記号は「、」「。」「？」だけ。アルファベット、箇条書き、Markdown は使わない。"

LOCAL_PROMPT = """あなたは、机の前にいる持ち主の音声アシスタント「レイ」です。返事はそのまま日本語で読み上げられます。
今は{now}です。
あなたが自分で答えてよいのは、あいさつ、お礼、ひとことの雑談、日付や時刻{data_ok}だけです。
画面つきで説明するのはクロードの仕事なので、一般知識、言葉の意味、計算、翻訳、相談、何かの説明やあらすじは、答えずに「@クロード」とだけ書いてください。
次のどれかに当てはまるときは、答えずに「@クロード」とだけ書いてください。ほかには何も書かないこと。
- {data_line}
- ウェブで調べる、最新の情報、ニュース、天気、店、値段が必要
- ファイルを書く、メモを追加する、コマンドを動かす、コードを書くなど、作業が必要
- 自信がない、詳しい説明を頼まれた、または長い説明が必要
{rules}{data}"""

GEMINI_PROMPT = """あなたは、机の前にいる持ち主の音声アシスタント「レイ」です。返事はそのまま日本語で読み上げられます。
今は{now}です。
一般知識、公開されている情報の説明、翻訳や言い換え、考え方の相談に、短く分かりやすく答えてください。
持ち主の予定、メモ、ファイル、お金などのデータが必要なとき、ウェブで最新の情報を調べる必要があるとき、作業が必要なときは、答えずに「@クロード」とだけ書いてください。
{rules}{data}"""

GEMINI_SEARCH_PROMPT = """あなたは、机の前にいる持ち主の音声アシスタント「レイ」です。返事はそのまま日本語で読み上げられます。
今は{now}です。
グーグル検索で調べて、分かったことの要点だけを答えてください。出典の名前は言ってよいが、URL は書かない。
持ち主の予定、メモ、ファイル、お金などのデータが必要なとき、ファイルの編集などの作業が必要なときは、答えずに「@クロード」とだけ書いてください。
{rules}"""

# 聞くまでもなく Claude に回すもの
# 持ち主のデータ（お金の詳細・仕事の機密・予定やメモ）：決めた線引きで、外（Gemini）には出さない
PRIVATE_RE = re.compile(
    r"予定|カレンダー|やること|タスク|家計|お金|残高|明細|給料|口座|カード|支払"
    r"|仕事|会社|職場|クライアント|顧客|機密"
    r"|デイリー|日記|メモ|ノート|インボックス|inbox",
    re.I,
)
# 作業（ファイル・コード・画面つきの返事）：道具を持っている Claude の仕事
WORK_RE = re.compile(
    r"追加|書いて|書き込|記録して|つけて|消して|削除|直して|編集|変えて|保存|コミット|プッシュ|push|git|ギット"
    r"|ファイル|フォルダ|cmd|コマンド|リポジトリ|プロジェクト|テスト"
    r"|コード|プログラム|スクリプト|関数|バグ"
    r"|天気|おはよう|朝のまとめ|画面|見せて|表に|図に|図で"  # 天気・朝のまとめは画面つきで答える
    r"|表示|キャプチャ|スクショ|スクリーンショット",  # 「サイトのキャプチャーを表示できる？」は Ray 自身の話（調べものではない）
    re.I,
)
# Claude の問いかけへの短い返事（「はい」「お願い」など）。前が Claude なら、続きとして Claude に回す
REPLY_RE = re.compile(r"^(はい|うん|ええ|いいよ|いいね|お願い|おねがい|頼む|たのむ|やって|そうして|それで|オッケー|OK|オーケー"
                      r"|いいえ|いや|ううん|やめて|やめとく|いらない|大丈夫|だいじょうぶ|結構|けっこう)", re.I)
REPLY_MAX = 15
# 調べもの：Gemini に Google 検索で答えさせる（だめなら Claude の WebSearch）
SEARCH_RE = re.compile(r"調べ|検索|ググ|ニュース|最新|最近の|今年|値段|価格|いくら|店|レストラン|ウェブ|サイト|発売|営業時間", re.I)
# 場所の言葉：「近くの家系ラーメン教えて」のように「調べて」と言わない店探しも、調べものとして扱う（足したルール）
NEAR_RE = re.compile(r"近く|近所|この辺|このへん|周辺|駅前|駅の近く|最寄り")
# 深い検索：いくつもの情報源を比べる・まとめる。これだけは、これまでどおり Gemini の検索か Claude へ
# （ふだんの検索は、検索 API → 要約の受け皿（search.py）。Gemini の検索枠は温存する）
DEEP_SEARCH_RE = re.compile(
    r"比べ|比較|違い|どっちが|どちらが|どれが(?:いい|良い|おすすめ)|メリット|デメリット|長所|短所"
    r"|徹底|網羅|いろいろ調べ|色々調べ|詳しく調べ|くわしく調べ|まとめて調べ|総合的|複数の"
)
# 体調・健康の記録：Ray が Cmd から読んでローカルに渡す（外には出さない）
HEALTH_RES = {
    "body": re.compile(r"体重|気分|体調|健康|調子|太|痩|やせ|減量"),
    "habits": re.compile(r"習慣|毎日確認|続いて|サボ"),
    "workout": re.compile(r"筋トレ|トレーニング|運動|ベンチ|スクワット|デッド"),
}
# 天気：Ray が先に取って、画面と読み上げの両方に同じ結果を渡す（天気は公開の情報なので外に出してよい）
WEATHER_RE = re.compile(r"天気|気温|降水|傘")
MORNING_RE = re.compile(r"おはよう|朝のまとめ")
# ローカルに任せるのは、画面のいらない、あいさつ・お礼・ひとことの雑談・時刻だけ
# （説明は画面つきで答えるので Claude へ。7B は「@クロード」と返す決まりを守らないことがあるので、言葉で決める）
CHAT_RE = re.compile(
    r"^(こんにちは|こんばんは|おはよ|おやすみ|ありがと|おつかれ|お疲れ|よろしく|ただいま|元気|やあ|はじめまして"
    r"|今何時|いま何時|何時|今日は何日|今日何日|何日|何曜日|今日は何曜日)"
)
CHAT_MAX = 20  # これより長い文は、あいさつで始まっても質問とみなす

# 参照先の指定（プロンプト変換で Claude に添える、先に見る Cmd のファイル）
# 振り分けルールの延長：言葉とファイルの対応で決める（7B の推定には頼らない。Notes/音声連携.md「プロンプト変換はコードで支える」）
# {today} は今日のデイリー（YYYY/MM/DD/デイリー.md）。明日・来週の水曜・11月20日などの日付は said_dates でその日のデイリーにする
# ほかのファイルは Cmd にあるものだけ添える
REF_RULES: list[tuple[re.Pattern, list[str]]] = [
    (re.compile(r"体重|減量|ダイエット|痩|やせ|太"), ["Notes/体の記録.md", "Projects/減量.md"]),
    (re.compile(r"気分|体調|調子"), ["{today}", "Notes/体の記録.md"]),
    (re.compile(r"筋トレ|トレーニング|ベンチ|スクワット|デッド|ジム"),
     ["Notes/筋トレの記録.md", "Notes/筋トレの計画.md", "Data/kintore/sets.csv"]),
    (re.compile(r"習慣|毎日確認|続いて|サボ"), ["Notes/毎日確認.md", "{today}"]),
    (re.compile(r"家計|残高|明細|支払|口座|カード|お金|使った|出費|支出|収入|給料"),
     ["Data/kakei.json", "Notes/家計の配分分析.md"]),
    (re.compile(r"貯金|貯め|引越|引っ越"), ["Projects/引越し貯金.md", "Notes/資産目標.md"]),
    (re.compile(r"資産|投資"), ["Notes/資産目標.md"]),
    (re.compile(r"副業|案件|クラウドワークス|ランサーズ"), ["Projects/副業.md"]),
    (re.compile(r"試験|応用情報|勉強"), ["Projects/応用情報技術者試験.md"]),
    (re.compile(r"服|クローゼット|コーデ"), ["Notes/クローゼット.md"]),
    (re.compile(r"レイ|Ray|音声|振り分け", re.I), ["Notes/音声連携.md"]),
    (re.compile(r"AIに頼|ＡＩに頼"), ["Notes/AIに頼むこと.md"]),
    (re.compile(r"インボックス|inbox", re.I), ["Inbox.md"]),
    (re.compile(r"やること|タスク|ToDo|todo", re.I), ["{today}", "Inbox.md"]),
    (re.compile(r"予定|カレンダー|今日|きょう|デイリー|日記|メモ"), ["{today}"]),
]
REF_MAX = 4  # 添えるファイルの数
_RELDAY_RE = re.compile(r"一昨日|おととい|明後日|あさって|今日|きょう|明日|あした|昨日|きのう")
_RELDAYS = {"一昨日": -2, "おととい": -2, "明後日": 2, "あさって": 2, "今日": 0, "きょう": 0,
            "明日": 1, "あした": 1, "昨日": -1, "きのう": -1}
_WEEKDAY_RE = re.compile(r"(今週|来週|再来週|先週)?の?([月火水木金土日])曜")
_MD_RE = re.compile(r"(?:(\d{4})[-/年])?(\d{1,2})[/月](\d{1,2})日?|(\d{4})-(\d{1,2})-(\d{1,2})")


def said_dates(text: str, now: datetime | None = None) -> list[str]:
    """発話の中の日付（明日、来週の水曜、11月20日、2026-11-20 など）を YYYY-MM-DD にする（出てきた順）"""
    t = unicodedata.normalize("NFKC", text)
    n = _now(now).date()
    out: list[tuple[int, str]] = []
    for m in _MD_RE.finditer(t):
        y, mo, da = (m.group(4), m.group(5), m.group(6)) if m.group(4) else (m.group(1), m.group(2), m.group(3))
        try:
            d = n.replace(year=int(y or n.year), month=int(mo), day=int(da))
        except ValueError:
            continue
        out.append((m.start(), d.isoformat()))
    for m in _RELDAY_RE.finditer(t):
        out.append((m.start(), (n + timedelta(days=_RELDAYS[m.group(0)])).isoformat()))
    for m in _WEEKDAY_RE.finditer(t):
        wd = WEEKDAYS.index(m.group(2))
        week = {"今週": 0, "来週": 1, "再来週": 2, "先週": -1}.get(m.group(1) or "")
        if week is None:  # 「水曜」だけ：今日から数えて次に来るその曜日
            d = n + timedelta(days=(wd - n.weekday()) % 7)
        else:
            d = n - timedelta(days=n.weekday()) + timedelta(days=7 * week + wd)
        out.append((m.start(), d.isoformat()))
    seen: list[str] = []
    for _, d in sorted(out):
        if d not in seen:
            seen.append(d)
    return seen


def ref_paths(text: str, cmd_dir: str | Path, now: datetime | None = None) -> list[str]:
    """発話の言葉から、Claude が先に見るとよい Cmd のファイルを決める（ルールの順。同じものは1回）"""
    root = Path(cmd_dir).expanduser()
    n = _now(now)
    days = {k: (n + timedelta(days=d)).strftime("%Y/%m/%d") + "/デイリー.md"
            for k, d in (("today", 0),)}
    found: list[str] = []

    def add(rel: str) -> None:
        if rel not in found and len(found) < REF_MAX:
            found.append(rel)

    dates = said_dates(text, n)
    for d in dates[:3]:  # 日付を言ったときは、その日のデイリー
        add(d.replace("-", "/") + "/デイリー.md")
    for rule, paths in REF_RULES:
        if rule.search(text):
            for p in paths:
                if p == "{today}" and dates:
                    continue  # 日付を言ったときは、その日のデイリーだけ（上で足した）
                if p.startswith("{"):
                    add(days[p.strip("{}")])  # デイリーは、まだ無くても添える（Claude が作る先）
                elif (root / p).exists():
                    add(p)
    # ノートやプロジェクトの名前をそのまま言ったときは、そのファイル
    for folder in ("Projects", "Notes"):
        d = root / folder
        if d.is_dir():
            for f in sorted(d.glob("*.md"), key=lambda f: -len(f.stem)):
                if len(f.stem) >= 2 and f.stem.lower() in text.lower():
                    add(f"{folder}/{f.name}")
    return found


def is_chat(text: str) -> bool:
    t = text.strip().lstrip("、。 ")
    return len(t) <= CHAT_MAX and bool(CHAT_RE.match(t))


# 詳しい説明は小さいモデルの苦手なので、ローカルを飛ばして Gemini へ
DEEP_RE = re.compile(r"詳しく|くわしく|詳細|しっかり説明")
_SENTENCE_END = re.compile(r"[。？！?!\n]")


# 体調の質問に出すパネル（先にあるほど優先。1つの返事に1枚）
HEALTH_PANELS = [
    ("workout", {"days": 90}),
    ("body", {"metric": "both", "days": 30}),
    ("habits", {"range": "week"}),
]


def panel_line(kind: str, params: dict) -> str:
    """画面の指示の行（app が読んでパネルを出す。読み上げはされない）"""
    return f"@画面 {kind} {json.dumps(params, ensure_ascii=False)}"


def health_panel(kinds: list[str]) -> str:
    for kind, params in HEALTH_PANELS:
        if kind in kinds:
            return panel_line(kind, params)
    return ""


def with_panel(on_text: Callable[[str], None], line: str) -> Callable[[str], None]:
    """最初の文を話すときに、パネルの指示を前につける（答えなかったときはパネルも出さない）"""
    state = {"first": bool(line)}

    def wrapped(sentence: str) -> None:
        if state["first"]:
            state["first"] = False
            on_text(f"{line}\n{sentence}")
        else:
            on_text(sentence)
    return wrapped


def is_weather(text: str) -> bool:
    """天気だけの質問か。予定や作業と混ざるとき、朝のまとめは、これまでどおり Claude"""
    return bool(WEATHER_RE.search(text) and not MORNING_RE.search(text) and not PRIVATE_RE.search(text)
                and not WORK_RE.search(text.replace("天気", "")))


def weather_date(text: str, now: datetime | None = None) -> str:
    dates = said_dates(text, now)
    return dates[0] if dates else _now(now).date().isoformat()


def weather_panel(text: str, now: datetime | None = None) -> str:
    return panel_line("weather", {"date": weather_date(text, now)})


def weather_text(cfg: dict, text: str, now: datetime | None = None) -> str:
    """画面と同じ天気の結果（同じ取得と使い回し）を、話す側に渡す短い文にする"""
    w = visuals.weather(cfg, {"date": weather_date(text, now), "every": 3})
    if w.get("error"):
        return f"{w['place']}の天気：取れませんでした。取れなかったと短く言う。天気の数字は作らない"
    hours = "、".join(f"{h['time'][:2]}時 {h['temp']}度 雨{h['pop']}パーセント {h['sky']}" for h in w["hours"])
    return (f"{w['place']}の{w['date']}の天気：いちばん高い気温 {w['max']}度、いちばん低い気温 {w['min']}度、"
            f"雨の確率は最大 {w['pop']}パーセント。3時間ごと：{hours}")


def needs_claude(text: str) -> bool:
    return bool(PRIVATE_RE.search(text) or WORK_RE.search(text))


def continues_claude(history: list[tuple[str, str, str]], text: str) -> bool:
    """直前に Claude が答えていて、それが問いかけ（？で終わる）か、今の発話が短い返事なら、続きとして Claude に回す
    （「調べて直しましょうか？」→「はい」を Gemini が受けると、話の流れを知らずに噛み合わない）"""
    if not history or history[-1][2] != "claude":
        return False
    if history[-1][1].rstrip().rstrip("」』）)").endswith(("？", "?")):
        return True
    t = text.strip().lstrip("、。 ")
    return len(t) <= REPLY_MAX and bool(REPLY_RE.match(t))


def needs_search(text: str) -> bool:
    return bool(SEARCH_RE.search(text) or NEAR_RE.search(text))


def needs_deep_search(text: str) -> bool:
    return bool(DEEP_SEARCH_RE.search(text))


def health_kinds(text: str) -> list[str]:
    return [k for k, r in HEALTH_RES.items() if r.search(text)]


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(JST)).astimezone(JST)


def _now_text(now: datetime | None = None) -> str:
    n = _now(now)
    return f"{n.year}年{n.month}月{n.day}日（{WEEKDAYS[n.weekday()]}）{n.hour}時{n.minute}分"


# ---------------------------------------------------------------- 持ち主の記録を文にする

def health_text(cfg: dict, kinds: list[str]) -> str:
    """体重・気分・習慣・筋トレの記録を、ローカルに渡す短い文にする（画面用の読み取りを使い回す）"""
    root = Path(cfg["cmd_dir"]).expanduser()
    out = []
    if "body" in kinds:
        b = visuals.body(root, {"days": 30})
        for s in b["series"]:
            pts = s["points"]
            if not pts:
                out.append(f"{s['name']}：この30日の記録なし")
                continue
            vals = "、".join(f"{p['date'][5:]} {p['value']}" for p in pts[-14:])
            change = f"（30日の変化 {s['change']:+}）" if "change" in s else ""
            out.append(f"{s['name']}（{s['unit']}）{change}：{vals}")
    if "habits" in kinds:
        h = visuals.habits(root, {"range": "week"})
        for r in h["habits"]:
            marks = "".join({"done": "○", "miss": "×", "none": "－", "off": "休"}[c["state"]] for c in r["cells"])
            out.append(f"習慣「{r['name']}」：この7日 {r['done']}／{r['due']}回、連続 {r['streak']}日、古い順に {marks}")
    if "workout" in kinds:
        w = visuals.workout(root, {"days": 90})
        if w.get("error"):
            out.append("筋トレ：記録なし")
        else:
            since = (visuals.today() - timedelta(days=30)).isoformat()
            with (root / "Data" / "kintore" / "sets.csv").open(encoding="utf-8") as f:
                days = sorted({r["date"] for r in csv.DictReader(f) if r.get("date", "") >= since})
            out.append(f"筋トレをした日（この30日で{len(days)}日）：" + ("、".join(d[5:] for d in days) or "なし"))
        for s in w.get("series", []):
            pts = "、".join(f"{p['date'][5:]} {p['weight']}キロ" for p in s["points"][-5:])
            out.append(f"筋トレ「{s['name']}」の最近の重さ：{pts}")
    return "\n".join(out)


# ---------------------------------------------------------------- 文ごとに読み上げる

class _Sentences:
    """流れてくる文字を文に区切って話す。最初の文の前に「@クロード」が来たら、他に回す合図"""

    def __init__(self, on_text: Callable[[str], None]):
        self.on_text = on_text
        self.spoken: list[str] = []
        self.buf = ""

    def feed(self, piece: str) -> str:
        """go（続ける）／handoff（まだ何も話していない。他に回す）／stop（ここで打ち切る）"""
        self.buf += piece
        if not self.spoken and HANDOFF in self.buf.replace(" ", ""):
            return "handoff"
        while (m := _SENTENCE_END.search(self.buf)):
            sentence, self.buf = self.buf[:m.end()], self.buf[m.end():]
            r = self._emit(sentence)
            if r != "go":
                return r
        return "go"

    def finish(self) -> str:
        r = self._emit(self.buf)
        self.buf = ""
        return r

    def _emit(self, sentence: str) -> str:
        s = sentence.strip()
        if not s:
            return "go"
        if "@" in s:
            return "stop" if self.spoken else "handoff"
        self.spoken.append(s)
        self.on_text(s)
        return "go"

    @property
    def answer(self) -> str | None:
        return " ".join(self.spoken) or None


def _read_stream(resp, out: _Sentences, piece_of: Callable[[str], str | None], who: str) -> str | None:
    """流れを最後まで（または打ち切りまで）読む。他に回すときは None"""
    r = "go"
    try:
        with resp:
            for raw in resp:
                line = raw.decode() if isinstance(raw, bytes) else raw
                piece = piece_of(line)
                if piece is None:  # 終わりの合図
                    break
                r = out.feed(piece)
                if r != "go":
                    break
        if r == "go":
            r = out.finish()
        return None if r == "handoff" else out.answer
    except Exception as e:  # 割り込みで閉じたときも、ここに来る
        if not out.spoken:
            print(f"{who}の返事が止まりました：{e}")
        return out.answer


class _Cancelable:
    def __init__(self):
        self.lock = threading.Lock()
        self.resp = None

    def _hold(self, resp) -> None:
        with self.lock:
            self.resp = resp

    def cancel(self) -> None:
        with self.lock:
            if self.resp is not None:
                try:
                    self.resp.close()
                except OSError:
                    pass


# ---------------------------------------------------------------- ローカル（Ollama）

class LocalBrain(_Cancelable):
    """Ollama の /api/chat を流しで読み、文ができるたびに返す"""

    def __init__(self, cfg: dict, urlopen=urllib.request.urlopen, clock=time.monotonic):
        super().__init__()
        self.c = cfg["local"]
        self.urlopen = urlopen
        self.clock = clock
        self.down_until = -1e9   # つながらなかったら、しばらく試さない
        self.warming = threading.Lock()

    def _url(self, path: str) -> str:
        return self.c["url"].rstrip("/") + path

    def _post(self, path: str, body: dict, timeout: float):
        req = urllib.request.Request(self._url(path), json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return self.urlopen(req, timeout=timeout)

    def loaded(self) -> bool:
        """モデルがメモリに載っているか（載っていなければ、最初の返事に30秒ほどかかる）"""
        if self.clock() < self.down_until:
            return False
        try:
            with self.urlopen(self._url("/api/ps"), timeout=1) as r:
                names = [m.get("name") for m in json.loads(r.read()).get("models", [])]
        except (OSError, ValueError):
            self.down_until = self.clock() + 30
            return False
        model = self.c["model"]
        return any(n == model or n == f"{model}:latest" for n in names)

    def warm(self, block: bool = False) -> None:
        """モデルを読み込んでおく（起動したとき・載っていなかったとき）"""
        def work():
            # 裏で読み込み中なら、block のときは終わるのを待つ（そうでなければ任せる）
            if not self.warming.acquire(blocking=block):
                return
            try:
                with self._post("/api/generate", {"model": self.c["model"], "prompt": "",
                                                  "keep_alive": self.c["keep_alive"]}, timeout=300) as r:
                    r.read()
            except OSError as e:
                print(f"ローカル LLM を読み込めませんでした（Gemini か Claude で答えます）：{e}")
                self.down_until = self.clock() + 30
            finally:
                self.warming.release()

        if block:
            work()
        else:
            threading.Thread(target=work, daemon=True).start()

    def unload(self) -> None:
        """メモリから外す（Ray を止めるとき）"""
        try:
            with self._post("/api/generate", {"model": self.c["model"], "keep_alive": 0}, timeout=5) as r:
                r.read()
        except OSError:
            pass

    def ask(self, history: list[tuple[str, str]], text: str, on_text: Callable[[str], None],
            data: str = "") -> str | None:
        """答えた文をつないで返す。他に回すべきときは None（まだ何も話していない）"""
        system = LOCAL_PROMPT.format(
            now=_now_text(), rules=SPEAK_RULES,
            data_ok="、下にある持ち主の記録についての質問" if data else "",
            data_line=("下の記録に無い、持ち主のほかのデータが必要" if data else
                       "持ち主のファイル、予定、やること、家計、体重、筋トレ、メモ、デイリーなど、持ち主のデータが必要"),
            data=f"\n持ち主の記録（Ray が読んだもの。これを元に答える。無いことは無いと言う）：\n{data}" if data else "",
        )
        messages = [{"role": "system", "content": system}]
        for q, a in history[-self.c["history"]:]:
            messages += [{"role": "user", "content": q}, {"role": "assistant", "content": a}]
        messages.append({"role": "user", "content": text})
        return self._chat(messages, on_text, self.c["model"], self.c["timeout"], self.c["keep_alive"])

    def _chat(self, messages: list[dict], on_text: Callable[[str], None], model: str, timeout: float,
              keep_alive) -> str | None:
        body = {"model": model, "stream": True, "keep_alive": keep_alive,
                "messages": messages, "options": {"temperature": self.c["temperature"], "num_ctx": self.c["num_ctx"]}}
        try:
            resp = self._post("/api/chat", body, timeout=timeout)
        except (OSError, ValueError) as e:
            print(f"ローカル LLM につながりません：{e}")
            self.down_until = self.clock() + 30
            return None
        self._hold(resp)

        def piece_of(line: str) -> str | None:
            if not line.strip():
                return ""
            ev = json.loads(line)
            return None if ev.get("done") and not ev.get("message", {}).get("content") else \
                ev.get("message", {}).get("content", "")

        try:
            return _read_stream(resp, _Sentences(on_text), piece_of, "ローカル LLM ")
        finally:
            self._hold(None)


# ---------------------------------------------------------------- Gemini 無料枠

class GeminiBrain(_Cancelable):
    """Gemini API（無料枠のみ）。上限に当たったら、次のリセット（日本時間17時）まで使わない"""

    API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:streamGenerateContent?alt=sse"

    def __init__(self, cfg: dict, urlopen=urllib.request.urlopen, now=lambda: datetime.now(JST),
                 env: dict | None = None, store: Path | None = None):
        super().__init__()
        self.c = cfg["gemini"]
        self.store = store       # 使った回数を残すファイル（Ray を起動し直しても数え直さない）
        self.urlopen = urlopen
        self.now = now
        env = os.environ if env is None else env
        self.key = self.c.get("api_key") or env.get("GEMINI_API_KEY") or env.get("GOOGLE_API_KEY") or ""
        self.day = None          # 枠の「日」（日本時間17時で切り替わる）
        self.count = 0
        self.search_count = 0    # そのうち検索つき（別の枠）
        self.exhausted_day = None
        self.search_exhausted_day = None  # 検索つきだけ上限に当たった日（ふつうの質問は使える）
        self._load()

    def _load(self) -> None:
        if not (self.store and self.store.exists()):
            return
        try:
            d = json.loads(self.store.read_text())
            day = datetime.fromisoformat(d["day"]).date()
        except (OSError, ValueError, KeyError, TypeError):
            return
        if day == self._quota_day():
            self.day, self.count = day, int(d.get("count", 0))
            self.search_count = int(d.get("search_count", 0))
            self.exhausted_day = day if d.get("exhausted") else None
            self.search_exhausted_day = day if d.get("search_exhausted") else None

    def _save(self) -> None:
        if not (self.store and self.day):
            return
        try:
            self.store.write_text(json.dumps({"day": self.day.isoformat(), "count": self.count,
                                              "search_count": self.search_count,
                                              "exhausted": self.exhausted_day == self.day,
                                              "search_exhausted": self.search_exhausted_day == self.day}))
        except OSError:
            pass

    def _quota_day(self):
        return (_now(self.now()) - timedelta(hours=self.c["reset_hour"])).date()

    def available(self) -> bool:
        if not (self.c["enabled"] and self.key):
            return False
        day = self._quota_day()
        if day != self.day:
            self.day, self.count, self.search_count = day, 0, 0
        return self.exhausted_day != day and self.count < self.c["daily_limit"]

    def search_available(self) -> bool:
        limit = self.c.get("search_daily_limit", 0)
        return (self.c["search"] and self.search_exhausted_day != self._quota_day()
                and not (limit and self.search_count >= limit))

    def ask(self, history: list[tuple[str, str]], text: str, on_text: Callable[[str], None],
            search: bool = False, data: str = "") -> str | None:
        if not self.available() or (search and not self.search_available()):
            return None
        self.count += 1
        if search:
            self.search_count += 1
        self._save()
        contents = []
        for q, a in history[-self.c["history"]:]:
            contents += [{"role": "user", "parts": [{"text": q}]}, {"role": "model", "parts": [{"text": a}]}]
        contents.append({"role": "user", "parts": [{"text": text}]})
        if search:
            system = GEMINI_SEARCH_PROMPT.format(now=_now_text(self.now()), rules=SPEAK_RULES)
        else:
            system = GEMINI_PROMPT.format(now=_now_text(self.now()), rules=SPEAK_RULES, data=(
                f"\n持ち主の体調・健康の記録（これを元に答える。無いことは無いと言う）：\n{data}" if data else ""))
        return self._run(system, contents, on_text, search)

    def summarize(self, system: str, history: list[tuple[str, str]], text: str,
                  on_text: Callable[[str], None]) -> str | None:
        """検索結果の要約（search.py の受け皿）。Google 検索は使わない（検索の枠を減らさない）"""
        if not self.available():
            return None
        self.count += 1
        contents = []
        for q, a in history[-self.c["history"]:]:
            contents += [{"role": "user", "parts": [{"text": q}]}, {"role": "model", "parts": [{"text": a}]}]
        contents.append({"role": "user", "parts": [{"text": text}]})
        return self._run(system, contents, on_text, search=False)

    def _run(self, system: str, contents: list[dict], on_text: Callable[[str], None], search: bool) -> str | None:
        self.last = "error"  # 終わり方：ok／handoff（自分では無理と「@クロード」を返した）／error
        body = {"systemInstruction": {"parts": [{"text": system}]},
                "contents": contents,
                "generationConfig": {"temperature": self.c["temperature"], "maxOutputTokens": 1024}}
        if search:
            body["tools"] = [{"google_search": {}}]  # グーグル検索で裏づけて答える
        req = urllib.request.Request(self.API.format(model=self.c["model"]), json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json", "x-goog-api-key": self.key})
        try:
            resp = self.urlopen(req, timeout=self.c["timeout"])
        except urllib.error.HTTPError as e:
            if e.code == 429 and search:
                # 検索つき（Google 検索での裏づけ）は、ふつうの質問と別の枠。ふつうの質問はそのまま使う
                print("Gemini の検索つきの無料枠の上限に当たりました（日本時間17時まで、調べものは Claude に回します）")
                self.search_exhausted_day = self._quota_day()
                self._save()
            elif e.code == 429:
                print("Gemini の無料枠の上限に当たりました（日本時間17時まで Claude に回します）")
                self.exhausted_day = self._quota_day()
                self._save()
            else:
                print(f"Gemini がエラーを返しました（{e.code}）")
            return None
        except (OSError, ValueError) as e:
            print(f"Gemini につながりません：{e}")
            return None
        self._hold(resp)
        sources: list[dict] = []

        def piece_of(line: str) -> str:
            if not line.startswith("data:"):
                return ""
            ev = json.loads(line[5:])
            cand = (ev.get("candidates") or [{}])[0]
            for ch in cand.get("groundingMetadata", {}).get("groundingChunks", []):
                web = ch.get("web") or {}
                if web.get("uri") and all(s["url"] != web["uri"] for s in sources):
                    title = web.get("title") or "出典"
                    sources.append({"title": title, "url": web["uri"], "site": title})
            parts = cand.get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts if not p.get("thought"))

        try:
            answer = _read_stream(resp, _Sentences(on_text), piece_of, "Gemini ")
        finally:
            self._hold(None)
        if answer and sources:
            # 調べたページの一覧を画面に出す（読み上げはしない）
            on_text(panel_line("sources", {"items": sources[: self.c["max_sources"]]}))
        self.last = "ok" if answer else ("handoff" if answer is None else "error")
        return answer


# ---------------------------------------------------------------- 振り分け

class HybridBrain:
    """Brain と同じ形（ask / cancel）。会話の番号は自分で持ち、Claude のセッションは中で対応づける"""

    def __init__(self, cfg: dict, claude, local: LocalBrain | None = None, gemini: GeminiBrain | None = None,
                 rewriter: Rewriter | None = None, search: SearchBrain | None = None):
        self.cfg = cfg
        self.claude = claude
        self.use_local = cfg["local"]["enabled"]
        self.local = local or LocalBrain(cfg)
        self.gemini = gemini or GeminiBrain(cfg)
        # Claude に回す前の、ローカルでのプロンプト変換（rewrite.py。長い発話・長い履歴のときだけ）
        self.rewriter = rewriter or Rewriter(cfg, urlopen=self.local.urlopen)
        # 検索の受け皿（search.py）：検索 API → Gemini 無料枠で要約 → だめなら Claude
        self.search = search or SearchBrain(cfg, self.gemini)
        self.conv: str | None = None
        self.claude_session: str | None = None
        self.history: list[tuple[str, str, str]] = []  # (質問, 答え, 答えた頭脳)
        self.unseen: list[tuple[str, str]] = []         # Claude がまだ知らない、ほかで答えたやり取り
        self.cut = threading.Event()

    def warm(self, block: bool = False) -> None:
        if self.use_local:
            self.local.warm(block=block)

    def close(self) -> None:
        if self.use_local:
            self.local.unload()

    def _local_ready(self) -> bool:
        if not self.use_local:
            return False
        if self.local.loaded():
            return True
        self.local.warm()  # 次から使えるように読み込んでおく
        return False

    def _done(self, text: str, answer: str, by: str, search: dict | None = None) -> Result:
        print(f"（{'ローカル' if by == 'local' else 'Gemini'}で答えました）")
        self.history.append((text, answer, by))
        self.unseen.append((text, answer))
        return Result(answer=answer, session_id=self.conv, spoken=[answer], by=by, search=search)

    def ask(self, text: str, on_text: Callable[[str], None], session_id: str | None = None,
            confirmed: bool = False) -> Result:
        self.cut.clear()
        search_log = None  # 調べものなら、使った検索の経路（ログ用）
        if not session_id or session_id != self.conv:
            self.conv = uuid.uuid4().hex
            self.claude_session, self.history, self.unseen = None, [], []
        everything = [(q, a) for q, a, _ in self.history]
        # 外（Gemini）に渡すのは、Claude が答えたもの（お金・予定などを含みうる）を除いたやり取りだけ
        outside_ok = [(q, a) for q, a, by in self.history if by != "claude"]

        if not confirmed and continues_claude(self.history, text):
            pass  # Claude との話の続き（問いかけへの返事）：下で Claude へ
        elif not confirmed and is_weather(text):
            # 天気：Ray が先に取って画面を作り、同じ結果を話す側へ。話す側が無理なら Claude へ
            data = weather_text(self.cfg, text)
            panel = weather_panel(text)
            if self._local_ready():
                answer = self.local.ask(everything, text, with_panel(on_text, panel), data=data)
                if answer:
                    return self._done(text, answer, "local")
            if not self.cut.is_set():
                answer = self.gemini.ask(outside_ok, text, with_panel(on_text, panel), data=data)
                if answer:
                    return self._done(text, answer, "gemini")
            if self.cut.is_set():
                return Result(answer="", session_id=self.conv, is_error=True, by="local")
        elif not confirmed and not needs_claude(text):
            kinds = health_kinds(text)
            if kinds:
                # 体調・健康の記録：Ray が読んでローカルへ。ローカルが無理なら Gemini、それも無理なら Claude
                data = health_text(self.cfg, kinds)
                panel = health_panel(kinds)
                if self._local_ready():
                    answer = self.local.ask(everything, text, with_panel(on_text, panel), data=data)
                    if answer:
                        return self._done(text, answer, "local")
                if not self.cut.is_set():
                    answer = self.gemini.ask(outside_ok, text, with_panel(on_text, panel), data=data)
                    if answer:
                        return self._done(text, answer, "gemini")
            elif needs_search(text):
                # 調べもの：ふだんは検索 API → Gemini 無料枠で要約。要約できなければ Claude
                # 深い検索と、検索 API が使えないときは、これまでどおり Gemini の検索。だめなら Claude（WebSearch）
                deep = needs_deep_search(text)
                if not deep:
                    found = self.search.ask(text, outside_ok, on_text)
                    search_log = found.log
                    if found.answer:
                        return self._done(text, found.answer, found.by, search=found.log)
                    if found.to_claude:
                        deep = None  # Gemini の検索も飛ばして Claude へ
                else:
                    search_log = {"route": "", "deep": True}
                if not self.cut.is_set() and deep is not None:
                    answer = self.gemini.ask(outside_ok, text, on_text, search=True)
                    if answer:
                        search_log.update(route="gemini_search")
                        return self._done(text, answer, "gemini", search=search_log)
                search_log.setdefault("route", "")
                search_log["route"] = search_log["route"] or "claude"
            else:
                if is_chat(text) and self._local_ready():
                    answer = self.local.ask(everything, text, on_text)
                    if answer:
                        return self._done(text, answer, "local")
                if not self.cut.is_set():
                    answer = self.gemini.ask(outside_ok, text, on_text)
                    if answer:
                        return self._done(text, answer, "gemini")
            if self.cut.is_set():
                return Result(answer="", session_id=self.conv, is_error=True, by="local")

        prompt = text
        if self.unseen and not confirmed:
            lines = "\n".join(f"持ち主：{q}\nレイ：{a}" for q, a in self.unseen)
            prompt = f"（ここまでのやり取り）\n{lines}\n（今の質問）\n{text}"
        # Claude 行きと決まったあと：長い発話・長い履歴なら、ローカルで短い依頼に変換する（失敗したら原文のまま）
        rw = None
        session = self.claude_session
        if not confirmed:
            # 参照先はコードのルールで決める（今の発話に無ければ、直前の質問から）
            # 下ごしらえした文で見る（言い直して消した日付や、呼びかけの「レイ」を拾わないため）
            now = self.rewriter.now()  # 変換と同じ「今」で日付を決める
            refs = ref_paths(tidy(text), self.cfg["cmd_dir"], now) or (
                ref_paths(tidy(everything[-1][0]), self.cfg["cmd_dir"], now) if everything else [])
            rw = self.rewriter.convert(text, everything, self.unseen, refs=refs,
                                       ready=lambda: self.use_local and self.local.loaded())
            if self.cut.is_set():
                return Result(answer="", session_id=self.conv, is_error=True, by="claude")
            if rw.prompt:
                prompt = rw.prompt
                if rw.fresh:
                    session = None  # 要約を渡して新しく始める（これまでの分を Claude に読み直させない）
                print(f"（ローカルで依頼を変換しました：{rw.log['before_chars']}→{rw.log['after_chars']}字）")
        res = self.claude.ask(prompt, on_text, session_id=session, confirmed=confirmed)
        res.rewrite = rw.log if rw else None
        res.search = search_log
        if res.session_id:
            self.claude_session = res.session_id
            self.unseen = []
        if res.answer and not res.is_error:
            self.history.append((text, res.answer, "claude"))
        res.session_id = self.conv
        res.by = "claude"
        return res

    def cancel(self) -> None:
        self.cut.set()
        self.local.cancel()
        self.gemini.cancel()
        self.rewriter.cancel()
        self.search.cancel()
        self.claude.cancel()
