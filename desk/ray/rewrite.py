"""プロンプト変換：Claude に回す前に、長い発話・長い履歴を短い依頼にする

Cmd の Notes/音声連携.md「7B によるプロンプト変換」「プロンプト変換はコードで支える（2026-11-19 決定）」の方針：
「抽出はコード → 整形は 7B → 検証はコード」。7B の足りないところは、前後をプログラムで固めて補う（14B は使わない）。

1. 抽出（コード）
   - 参照先：言葉と Cmd のファイルの対応ルールで決める（local.py の REF_RULES・ref_paths。振り分けルールの延長）
   - 下ごしらえ：言いよどみ・呼びかけ・くり返し・言い直しを、モデルに渡す前にルールで落とす（tidy）
2. 整形（7B）：意味を保った言い換えと、履歴の要約だけ。Ollama の format（JSON スキーマ）で形を固定する
3. 検証（コード）：変換前の数字・日付・英語の名前が、変換後にも残っているか照合する（facts）
   - 欠けていたら、原文を併記して Claude へ送る
   - 壊れた JSON・空・日本語でない・元に無い数字を作った・時間切れなどは、原文のまま送る（変換で会話を止めない）

変換はローカル（Ollama）だけで行い、Mac の外には出さない。
履歴を要約したときは、Claude のセッションを続けずに（--resume しない）新しく始め、要約を渡す。
続けると、Claude はこれまでのやり取りと読んだファイルを毎回読み直すため（未明の実績で消費の大半）。
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
HISTORY_TURNS = 12          # 要約に渡す直前のやり取りの数
ANSWER_CHARS = 300          # 要約に渡す答え1つあたりの字数
NUM_PREDICT = 300           # 変換の出力の上限（トークン）

INSTRUCTION_MAX = 120       # 指示文の字数の目安（モデルへの指示）
SUMMARY_MAX = 200           # 要約の字数の目安（モデルへの指示）

KEYS = ("enabled", "min_chars", "min_history_turns", "min_history_chars", "timeout", "fresh_session")

# Ollama の format（構造化出力）。7B の出力の形をここで固定する
SCHEMA = {
    "type": "object",
    "properties": {"依頼": {"type": "string"}, "要約": {"type": "string"}},
    "required": ["依頼", "要約"],
}

REWRITE_PROMPT = """あなたは、音声アシスタント「レイ」の下書き係です。持ち主の発話を、クロードに渡す短い依頼の文に言い換えます。あなたは質問に答えません。
今は__NOW__です。
出すのは JSON だけ：{"依頼": "…", "要約": "…"}
- 依頼：今の発話を、意味を変えずに、簡潔な指示文に言い換える。頼まれていないことは足さない。__INSTRUCTION_MAX__字以内
- 要約：これまでのやり取りのうち、今の依頼に関係することだけを短くまとめる。__SUMMARY_MAX__字以内。やり取りが無ければ空の文字列
決まり（いちばん大事）
- 数字、金額、日付、曜日、時刻、名前、固有名詞は、一つも落とさず、書いてあるとおりに写す。数字を漢数字に変えない
- 「さっきのやつ」「それ」などは、これまでのやり取りから何のことかを書く
- 日本語だけで書く。中国語や英語に訳さない。発話にある英語の名前だけは、そのまま書いてよい
例
発話：来週の水曜の19時からの飲み会、誰と行くんだっけ、メモに書いた、ちょっと見てくれる？
出力：{"依頼": "来週水曜19時からの飲み会に誰と行くか、メモを見て答える", "要約": ""}
"""

# ---------------------------------------------------------------- 1. 下ごしらえ（コード）

_ADDRESS = re.compile(r"^\s*(?:ねえ|ねぇ|おい)?[、,\s]*(?:レイ|れい|Ray)[、,\s]+", re.I)
_FILLERS = re.compile(
    r"(?:え[ー〜]+っと|えっと|え[ー〜]+と|ええと)(?:さ|ね)?[、,]?|え[ー〜]+[、,]|あの[ー〜]+(?:さ|ね)?[、,]?|あのね[、,]?|あの[、,]"
    r"|う[ー〜]+ん[、,]?|うんと[、,]|なんか[、,]|まあ[、,]|まぁ[、,]|その[ー〜]+[、,]?|ほら[、,]|ちょっと[、,]"
)
_HEDGES = re.compile(r"(?:と思うんだけど|と思うんですけど|なんだけど|なんですけど|んだけど|んですけど)(?:さ|ね)?(?=[、,。？?]|$)")
_CORRECTION = re.compile(r"(^|[、。])[^、。]{1,10}[、,]\s*いや[、,\s]*")   # 「水曜、いや木曜の」→「木曜の」
_REPEAT_COMMA = re.compile(r"([^、。]{2,12})[、,]\s*\1")                 # 「今日の、今日の」→「今日の」
_REPEAT = re.compile(r"([^\d\s、。]{3,8}?)\1+")                          # 「今日の今日の」→「今日の」
_COMMAS = re.compile(r"[、,]\s*(?=[、,。？?])|^[、,\s]+|[、,\s]+$")


def tidy(text: str) -> str:
    """モデルに渡す前に、言いよどみ・呼びかけ・くり返し・言い直しを落とす（数字は消さない）"""
    t = _ADDRESS.sub("", text.strip())
    t = _FILLERS.sub("", t)
    t = _HEDGES.sub("", t)
    t = _CORRECTION.sub(r"\1", t)
    t = _REPEAT_COMMA.sub(r"\1", t)
    t = _REPEAT.sub(r"\1", t)
    t = re.sub(r"[、,]{2,}", "、", t)
    t = _COMMAS.sub("", t)
    return t.strip() or text.strip()


# ---------------------------------------------------------------- 3. 検証（コード）

_KANA = re.compile(r"[぀-ヿ]")
_LATIN = re.compile(r"[A-Za-z][A-Za-z0-9._\-]+")
_BAD = re.compile(r"[@＠](?:クロード|画面)|[가-힯]")  # 振り分けの合図・画面の指示・ハングル
_ARABIC = re.compile(r"\d+(?:\.\d+)?(万)?")
_KANJI_NUM = re.compile(r"(?<![\d〇零一二三四五六七八九十百千万])([〇零一二三四五六七八九十百千万]*[〇零一二三四五六七八九十百千][〇零一二三四五六七八九十百千万]*)(?=時|分|日|月|年|円|キロ|回|人|個|件|週|歳|秒|ページ|問|点|倍|か月|ヶ月|カ月)")
_KANA_DAYS = {"あした": "明日", "きょう": "今日", "きのう": "昨日", "あさって": "明後日", "おととい": "一昨日"}
_DAY_WORDS = re.compile(r"一昨日|明後日|今日|明日|昨日|再来週|今週|来週|先週|今月|来月|先月|今年|来年|去年|週末|平日|午前|午後")
_WEEKDAY = re.compile(r"([月火水木金土日])曜")
_KD = {c: i for i, c in enumerate("〇一二三四五六七八九")}
_KD["零"] = 0


def _kanji_int(s: str) -> int | None:
    total, section, digit = 0, 0, None
    for ch in s:
        if ch in _KD:
            digit = _KD[ch] if digit is None else digit * 10 + _KD[ch]
        elif ch in "十百千":
            section += (digit if digit is not None else 1) * {"十": 10, "百": 100, "千": 1000}[ch]
            digit = None
        elif ch == "万":
            total += (section + (digit or 0)) * 10000
            section, digit = 0, None
        else:
            return None
    return total + section + (digit or 0)


def _num(s: str) -> str:
    """数の書き方をそろえる（"01" と "1"、"70.50" と "70.5" は同じ）"""
    if "." in s:
        return s.rstrip("0").rstrip(".")
    return str(int(s))


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    for k, v in _KANA_DAYS.items():
        t = t.replace(k, v)
    return t


def numbers(text: str) -> list[str]:
    t = _norm(text)
    out = []
    for m in _ARABIC.finditer(t):
        v = m.group(0).rstrip("万")
        out.append(str(int(float(v) * 10000)) if m.group(1) else _num(v))
    for m in _KANJI_NUM.finditer(t):
        v = _kanji_int(m.group(1))
        if v is not None:
            out.append(str(v))
    return out


def facts(text: str) -> list[str]:
    """落としてはいけない固有情報：数字（漢数字も数に直す）・日付の言葉・曜日・英語の名前"""
    t = _norm(text)
    out = [f"数 {n}" for n in numbers(t)]
    out += [f"日 {m.group(0)}" for m in _DAY_WORDS.finditer(t)]
    out += [f"曜 {m.group(1)}" for m in _WEEKDAY.finditer(t)]
    out += [f"名 {w.lower()}" for w in _LATIN.findall(t)]
    seen: list[str] = []
    for f in out:
        if f not in seen:
            seen.append(f)
    return seen


def missing_facts(before: str, after: str) -> list[str]:
    """変換前にあって、変換後に無い固有情報"""
    have = set(facts(after))
    return [f for f in facts(before) if f not in have]


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


def check(obj: dict | None, question: str, source: str, need_summary: bool) -> tuple[dict | None, str]:
    """7B の出力を確かめる。使えなければ (None, 理由)。使えれば (結果, "")。結果の missing は欠けた固有情報"""
    if obj is None:
        return None, "invalid_json"
    inst = str(obj.get("依頼") or obj.get("instruction") or "").strip()
    summ = str(obj.get("要約") or obj.get("summary") or "").strip()
    if not inst:
        return None, "empty"
    if not _KANA.search(inst) or (summ and not _KANA.search(summ)):
        return None, "not_japanese"   # かなが無い＝中国語か英語になっている
    if _BAD.search(inst) or _BAD.search(summ):
        return None, "bad_marker"
    src = _norm(source).lower()
    if any(w.lower() not in src for w in _LATIN.findall(_norm(inst + " " + summ))):
        return None, "not_japanese"   # 元に無い英語が出てきた
    if set(numbers(inst + " " + summ)) - set(numbers(source)):
        return None, "invented_number"  # 元に無い数字を作った（取り違え）
    if len(inst) > max(len(question) * 1.2, len(question) + 40):
        return None, "too_long"
    if need_summary and not summ:
        return None, "no_summary"
    return {"instruction": inst, "summary": summ, "missing": missing_facts(question, inst)}, ""


# ---------------------------------------------------------------- 組み立て

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


def build_prompt(parts: dict, refs: list[str], include_summary: bool, original: str = "") -> str:
    """Claude に渡す依頼の文。original があれば併記する（まとめで固有情報が欠けたとき）"""
    lines = ["（Ray が持ち主の話を短くまとめた依頼。足りなければ調べてください）"]
    if include_summary and parts["summary"]:
        lines.append(f"これまでの話：{parts['summary']}")
    lines.append(f"依頼：{parts['instruction']}")
    if refs:
        lines.append("先に見るファイル：" + "、".join(refs))
    if original:
        lines.append(f"持ち主の言葉（まとめで抜けた所があるので、こちらを正とする）：{original}")
    return "\n".join(lines)


def settings(cfg: dict) -> dict:
    """モジュールの定数を既定にして、config.toml の [rewrite] で上書きする"""
    s = {k: globals()[k.upper()] for k in KEYS}
    s.update({k: v for k, v in (cfg.get("rewrite") or {}).items() if k in KEYS})
    return s


def _now_text(now: datetime) -> str:
    n = now.astimezone(JST)
    return f"{n.year}年{n.month}月{n.day}日（{WEEKDAYS[n.weekday()]}）{n.hour}時{n.minute}分"


@dataclass
class Rewrite:
    prompt: str | None = None     # Claude に渡す文（変換しないとき・失敗したときは None ＝原文のまま）
    fresh: bool = False           # Claude のセッションを新しく始めるか
    log: dict = field(default_factory=dict)


# ---------------------------------------------------------------- 2. 整形（7B）

class Rewriter:
    """Ollama の /api/chat（流さない・JSON スキーマで形を固定）で、言い換えと要約だけをさせる"""

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
                refs: list[str] | None = None, ready: Callable[[], bool] = lambda: True) -> Rewrite:
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
        log["before"] = original
        if not ready():
            log["error"] = "local_not_ready"
            return Rewrite(log=log)
        long_history = "long_history" in reason
        refs = list(refs or [])
        # 1. 下ごしらえ（コード）。言い直し（「水曜、いや木曜」）の訂正も含むので、検証はこの文と比べる
        tidied = tidy(text)
        log["tidied"] = tidied
        start = self.clock()
        try:
            raw = self._call(tidied, history if long_history else [], s["timeout"])
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
        # 3. 検証（コード）
        source = text + "\n" + history_text(history)
        parts, why = check(_parse(raw), tidied, source, need_summary=long_history)
        if parts is None:
            log["error"] = why
            log["raw"] = raw[:500]
            return Rewrite(log=log)
        missing = parts["missing"]
        fresh = long_history and s["fresh_session"]
        body = build_prompt(parts, refs, include_summary=fresh, original=text if missing else "")
        # 続けるときは、履歴は今の Claude のセッションが知っている。知らない分だけ、これまでどおり添える
        prompt = body if fresh else unseen_prompt(unseen, body)
        log.update(applied=True, after=prompt, after_chars=len(prompt), fresh_session=fresh, refs=refs,
                   instruction=parts["instruction"], summary=parts["summary"],
                   missing=missing, with_original=bool(missing))
        return Rewrite(prompt=prompt, fresh=fresh, log=log)

    def _call(self, text: str, history: list[tuple[str, str]], timeout: float) -> str | None:
        """別の糸で呼び、timeout 秒で打ち切る（ソケットの待ちだけでは全体の時間を区切れないため）"""
        self.cancelled.clear()
        self.done.clear()
        system = (REWRITE_PROMPT
                  .replace("__NOW__", _now_text(self.now()))
                  .replace("__INSTRUCTION_MAX__", str(INSTRUCTION_MAX))
                  .replace("__SUMMARY_MAX__", str(SUMMARY_MAX)))
        user = (f"（これまでのやり取り）\n{history_text(history)}\n" if history else "") + f"（今の発話）\n{text}"
        body = {"model": self.c["model"], "stream": False, "format": SCHEMA, "keep_alive": self.c["keep_alive"],
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
