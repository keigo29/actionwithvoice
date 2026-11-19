"""プロンプト変換：Claude に回す前に、ローカル LLM（Ollama の 7B）が入力を短い依頼に書き直す

Cmd の Notes/音声連携.md「7B によるプロンプト変換（2026-11-19）」の方針：
- 目的は Claude 側の消費（ファイルの探し回りと、履歴の読み直し）を減らすこと
- 変換の中身は3つ。質問の圧縮（話し言葉を簡潔な指示文に）、履歴の要約、参照先の指定（Cmd のどのファイルを見るか）
- 数字・名前・日付などの固有情報は落とさない。出力は日本語だけ
- 短い質問には変換をかけない（変換の時間の方が高くつく）。長い発話と、履歴が長いときだけ
- 変換はローカルで完結させる（Mac の外には出さない）。失敗・空・おかしい・時間切れは、原文のまま Claude へ

履歴を要約したときは、Claude のセッションを続けずに（--resume しない）新しく始め、要約を渡す。
続けると、Claude はこれまでのやり取りとファイルの中身を毎回読み直すため（未明の実績で消費の大半）。
"""

from __future__ import annotations

import json
import re
import threading
import time
import unicodedata
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from . import scenes

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

# ---------------------------------------------------------------- 調整できる値（config.toml の [rewrite] で上書きできる）

ENABLED = True
MIN_CHARS = 60              # 発話がこの字数以上なら「長い発話」として変換する
MIN_HISTORY_TURNS = 4       # 同じ会話のやり取りがこの数以上なら「履歴が長い」
MIN_HISTORY_CHARS = 1200    # または、やり取りの字数の合計がこれ以上なら「履歴が長い」
TIMEOUT = 10                # 変換にかけてよい秒数。超えたら原文で送る
FRESH_SESSION = True        # 履歴を要約したら、Claude のセッションを新しく始める（読み直しを減らす）
MAX_REFS = 3                # 添える参照先の数
HISTORY_TURNS = 12          # 要約に渡す直前のやり取りの数
ANSWER_CHARS = 300          # 要約に渡す答え1つあたりの字数
NUM_PREDICT = 400           # 変換の出力の上限（トークン）

INSTRUCTION_MAX = 120       # 指示文の字数の目安（モデルへの指示）
SUMMARY_MAX = 200           # 要約の字数の目安（モデルへの指示）

KEYS = ("enabled", "min_chars", "min_history_turns", "min_history_chars", "timeout", "fresh_session")

REWRITE_PROMPT = """あなたは、音声アシスタント「レイ」の下書き係です。持ち主の話し言葉と、これまでのやり取りを、クロードに渡す短い依頼に書き直します。あなたは質問に答えません。
今は__NOW__です。
次の形の JSON だけを出してください。ほかには何も書かないこと。
{"依頼": "…", "要約": "…", "参照": ["…"]}
- 依頼：今の発話を、簡潔な指示文に直す。言いよどみ、あいづち、くり返しは消す。頼まれていないことは足さない。__INSTRUCTION_MAX__字以内
- 要約：これまでのやり取りのうち、今の依頼に関係することだけを短くまとめる。__SUMMARY_MAX__字以内。やり取りが無ければ空の文字列
- 参照：クロードが先に見るとよい Cmd のファイルを、下の一覧から__MAX_REFS__つまで。一覧にないものは書かない。分からなければ空の配列
決まり（いちばん大事）
- 数字、金額、名前、日付、時刻、曜日、ファイル名、固有名詞は、一つも落とさず、そのまま書く
- 日本語だけで書く。中国語や英語に訳さない。発話にある英語の名前だけは、そのまま書いてよい
例
発話：えーっとさ、あのー、来週の水曜の19時からの飲み会、あれって誰と行くんだっけ、メモに書いたと思うんだけど
出力：{"依頼": "来週水曜19時からの飲み会に誰と行くか、メモから調べて答える", "要約": "", "参照": ["__DAILY__"]}
Cmd のファイルの一覧（→ の後は中身の目安）：
__FILES__
"""

# ファイルの中身の目安（一覧に添えて、7B が選びやすくする）
HINTS = {
    "Inbox.md": "行き先の決まっていないやること",
    "README.md": "書き方の決まり",
    "CLAUDE.md": "AI 向けの決まり",
    "Data/kakei.json": "家計のデータ",
    "Data/kintore": "筋トレの記録（sets.csv）",
    "Data/health": "体重・歩数などの健康データ",
    "Data/closet": "クローゼットのデータ",
    "Data/memo": "メモアプリから取り込んだメモ",
    "Data/ray": "Ray との会話の記録",
    "Data/voice": "iPhone の音声メモ",
}

_KANA = re.compile(r"[぀-ヿ]")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9._\-]+")
_DIGITS = re.compile(r"\d+")
_BAD = re.compile(r"[@＠](?:クロード|画面)|[가-힯]")  # 振り分けの合図・画面の指示・ハングル


def settings(cfg: dict) -> dict:
    """モジュールの定数を既定にして、config.toml の [rewrite] で上書きする"""
    s = {k: globals()[k.upper()] for k in KEYS}
    s.update({k: v for k, v in (cfg.get("rewrite") or {}).items() if k in KEYS})
    return s


def _now(now: datetime | None = None) -> datetime:
    return (now or datetime.now(JST)).astimezone(JST)


def _now_text(now: datetime | None = None) -> str:
    n = _now(now)
    return f"{n.year}年{n.month}月{n.day}日（{WEEKDAYS[n.weekday()]}）{n.hour}時{n.minute}分"


def _norm(s: str) -> str:
    return unicodedata.normalize("NFKC", s)


def daily_path(now: datetime | None = None) -> str:
    return _now(now).strftime("%Y/%m/%d") + "/デイリー.md"


def ref_candidates(cmd_dir: str | Path, now: datetime | None = None) -> dict[str, str]:
    """参照先に選んでよいファイル（パス → 中身の目安）。7B が勝手なパスを作らないよう、一覧から選ばせる"""
    root = Path(cmd_dir).expanduser()
    n = _now(now)
    out: dict[str, str] = {}

    def add(rel: str, hint: str = "") -> None:
        out.setdefault(rel, hint or HINTS.get(rel, ""))

    add(daily_path(n), "今日のデイリー（今日のやること・予定・メモ・気分・体重）")
    add(daily_path(n - timedelta(days=1)), "昨日のデイリー")
    day = root / n.strftime("%Y/%m/%d")
    if day.is_dir():
        for p in sorted(day.glob("*.md")):
            add(p.relative_to(root).as_posix(), "今日のノート")
    for name in ("Inbox.md", "README.md"):
        if (root / name).exists():
            add(name)
    for folder in ("Notes", "Projects"):
        for p in sorted((root / folder).glob("*.md")):
            add(p.relative_to(root).as_posix(), "プロジェクト（やること・期限）" if folder == "Projects" else "")
    data = root / "Data"
    if data.is_dir():
        for p in sorted(data.iterdir()):
            if not p.name.startswith("."):
                add(p.relative_to(root).as_posix())
    return out


def history_text(history: list[tuple[str, str]], turns: int = HISTORY_TURNS, answer_chars: int = ANSWER_CHARS) -> str:
    lines = []
    for q, a in history[-turns:]:
        a = scenes.strip_directives(a) or a
        if len(a) > answer_chars:
            a = a[:answer_chars] + "…"
        lines.append(f"持ち主：{q}\nレイ：{a}")
    return "\n".join(lines)


def history_chars(history: list[tuple[str, str]]) -> int:
    return sum(len(q) + len(scenes.strip_directives(a) or a) for q, a in history)


def unseen_prompt(unseen: list[tuple[str, str]], text: str) -> str:
    """Claude がまだ知らないやり取りを添えた文（変換しないときの、これまでどおりの形）"""
    if not unseen:
        return text
    lines = "\n".join(f"持ち主：{q}\nレイ：{a}" for q, a in unseen)
    return f"（ここまでのやり取り）\n{lines}\n（今の質問）\n{text}"


def _parse(raw: str) -> dict | None:
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", raw, re.S)  # 前後に余計な文がついたとき
        if not m:
            return None
        try:
            obj = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def check(obj: dict | None, question: str, source: str, candidates: dict[str, str],
          need_summary: bool) -> tuple[dict | None, str]:
    """変換の結果を確かめる。おかしければ (None, 理由)。よければ (整えた結果, "")"""
    if obj is None:
        return None, "invalid_json"
    inst = str(obj.get("依頼") or obj.get("instruction") or "").strip()
    summ = str(obj.get("要約") or obj.get("summary") or "").strip()
    refs = obj.get("参照") or obj.get("refs") or []
    if not inst:
        return None, "empty"
    if not _KANA.search(inst) or (summ and not _KANA.search(summ)):
        return None, "not_japanese"   # かなが無い＝中国語か英語になっている
    if _BAD.search(inst) or _BAD.search(summ):
        return None, "bad_marker"
    # 固有情報：発話の数字と英語の名前は、指示文に全部残っていること
    q, i = _norm(question), _norm(inst)
    if any(d not in i for d in _DIGITS.findall(q)):
        return None, "lost_number"
    il = i.lower()
    if any(w.lower() not in il for w in _LATIN.findall(q)):
        return None, "lost_name"
    # 英語に流れていないか：指示文・要約の英語は、もとの発話かやり取りにあるものだけ
    src = _norm(source).lower()
    if any(w.lower() not in src for w in _LATIN.findall(_norm(inst + " " + summ))):
        return None, "not_japanese"
    # 縮んでいない（話をふくらませた）ものは使わない
    if len(inst) > max(len(question) * 1.2, len(question) + 40):
        return None, "too_long"
    if need_summary and not summ:
        return None, "no_summary"
    if not isinstance(refs, list):
        refs = [refs]
    clean = []
    for r in refs:
        r = str(r).strip().lstrip("/").removeprefix("Cmd/")
        if r in candidates and r not in clean:
            clean.append(r)
    return {"instruction": inst, "summary": summ, "refs": clean[:MAX_REFS]}, ""


def build_prompt(parts: dict, include_summary: bool) -> str:
    """Claude に渡す依頼の文"""
    lines = ["（Ray が持ち主の話を短くまとめた依頼。足りなければ調べてください）"]
    if include_summary and parts["summary"]:
        lines.append(f"これまでの話：{parts['summary']}")
    lines.append(f"依頼：{parts['instruction']}")
    if parts["refs"]:
        lines.append("先に見るファイル：" + "、".join(parts["refs"]))
    return "\n".join(lines)


@dataclass
class Rewrite:
    prompt: str | None = None     # 変換した依頼（変換しないとき・失敗したときは None）
    fresh: bool = False           # Claude のセッションを新しく始めるか
    log: dict = field(default_factory=dict)


class Rewriter:
    """Ollama の /api/chat（流さない・JSON で出させる）で、依頼を書き直す"""

    def __init__(self, cfg: dict, urlopen=urllib.request.urlopen, clock=time.monotonic,
                 now: Callable[[], datetime] | None = None):
        self.cfg = cfg
        self.c = cfg["local"]
        self.urlopen = urlopen
        self.clock = clock
        self.now = now or (lambda: datetime.now(JST))
        self.lock = threading.Lock()
        self.resp = None
        self.cancelled = threading.Event()
        self.done = threading.Event()

    def should(self, text: str, history: list[tuple[str, str]]) -> str:
        """変換する理由（long_utterance / long_history / 両方を + でつなぐ）。しないときは空"""
        s = settings(self.cfg)
        if not s["enabled"]:
            return ""
        reasons = []
        if len(text.strip()) >= s["min_chars"]:
            reasons.append("long_utterance")
        if history and (len(history) >= s["min_history_turns"] or history_chars(history) >= s["min_history_chars"]):
            reasons.append("long_history")
        return "+".join(reasons)

    def convert(self, text: str, history: list[tuple[str, str]], unseen: list[tuple[str, str]],
                ready: Callable[[], bool] = lambda: True) -> Rewrite:
        """変換した依頼を返す。どんな失敗でも例外は出さず、prompt=None（原文で送る）にする"""
        s = settings(self.cfg)
        original = unseen_prompt(unseen, text)
        log = {"applied": False, "question_chars": len(text), "history_turns": len(history),
               "history_chars": history_chars(history), "before_chars": len(original)}
        reason = self.should(text, history)
        if not reason:
            log["reason"] = "short" if s["enabled"] else "disabled"
            return Rewrite(log=log)
        log["reason"] = reason
        if not ready():
            log["error"] = "local_not_ready"
            return Rewrite(log=log)
        long_history = "long_history" in reason
        start = self.clock()
        try:
            candidates = ref_candidates(self.cfg["cmd_dir"], self.now())
            raw = self._call(text, history if long_history else [], candidates, s["timeout"])
        except TimeoutError:
            log["error"] = "timeout"
            raw = None
        except Exception as e:  # 変換で会話は止めない
            log["error"] = f"error: {type(e).__name__}"
            raw = None
        log["ms"] = int((self.clock() - start) * 1000)
        if raw is None:
            log.setdefault("error", "cancelled" if self.cancelled.is_set() else "empty")
            return Rewrite(log=log)
        source = text + "\n" + history_text(history) + "\n" + "\n".join(candidates)  # 一覧のパスは英字でもよい
        parts, why = check(_parse(raw), text, source, candidates, need_summary=long_history)
        if parts is None:
            log["error"] = why
            log["raw"] = raw[:500]
            return Rewrite(log=log)
        fresh = long_history and s["fresh_session"]
        if fresh:
            prompt = build_prompt(parts, include_summary=True)
        else:
            # 履歴は今の Claude のセッションが知っている。知らない分だけ、これまでどおり添える
            prompt = unseen_prompt(unseen, build_prompt(parts, include_summary=False))
        log.update(applied=True, after_chars=len(prompt), fresh_session=fresh, refs=parts["refs"],
                   instruction=parts["instruction"], summary=parts["summary"])
        return Rewrite(prompt=prompt, fresh=fresh, log=log)

    def _call(self, text: str, history: list[tuple[str, str]], candidates: dict[str, str],
              timeout: float) -> str | None:
        """別の糸で呼び、timeout 秒で打ち切る（ソケットの待ちだけでは全体の時間を区切れないため）"""
        self.cancelled.clear()
        self.done.clear()
        system = (REWRITE_PROMPT
                  .replace("__NOW__", _now_text(self.now()))
                  .replace("__INSTRUCTION_MAX__", str(INSTRUCTION_MAX))
                  .replace("__SUMMARY_MAX__", str(SUMMARY_MAX))
                  .replace("__MAX_REFS__", str(MAX_REFS))
                  .replace("__DAILY__", daily_path(self.now()))
                  .replace("__FILES__", "\n".join(f"- {p}" + (f" → {h}" if h else "") for p, h in candidates.items())))
        user = (f"（これまでのやり取り）\n{history_text(history)}\n" if history else "") + f"（今の発話）\n{text}"
        body = {"model": self.c["model"], "stream": False, "format": "json", "keep_alive": self.c["keep_alive"],
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "options": {"temperature": 0, "num_ctx": self.c["num_ctx"], "num_predict": NUM_PREDICT}}
        req = urllib.request.Request(self.c["url"].rstrip("/") + "/api/chat", json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        box: dict = {}

        def work():
            try:
                resp = self.urlopen(req, timeout=timeout)
                with self.lock:
                    self.resp = resp
                with resp:
                    box["raw"] = json.loads(resp.read()).get("message", {}).get("content", "")
            except Exception as e:
                box["error"] = e
            finally:
                with self.lock:
                    self.resp = None
                self.done.set()

        threading.Thread(target=work, daemon=True).start()
        if not self.done.wait(timeout):
            self.cancel()
            raise TimeoutError
        if self.cancelled.is_set():
            return None
        if "error" in box:
            raise box["error"]
        return box.get("raw")

    def cancel(self) -> None:
        self.cancelled.set()
        self.done.set()  # 待っている側をすぐ起こす
        with self.lock:
            if self.resp is not None:
                try:
                    self.resp.close()
                except OSError:
                    pass
