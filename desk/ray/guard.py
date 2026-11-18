"""読み上げの直前の守り：日本語だけ・記号なし・6文まで

指示文（brain.VOICE_PROMPT）で頼んでも、Claude はたまに決まりを破る。
ここで最後に直してから読み上げる。
"""

from __future__ import annotations

import re

MAX_SENTENCES = 6
OPENER_MAX = 12  # この字数までのひとことは、最初の1回だけ数えない

# 英語の名前の読み方（長いものから順に置き換える）
READINGS = {
    "マネーフォワードME": "マネーフォワード",
    "Money Forward ME": "マネーフォワード",
    "Money Forward": "マネーフォワード",
    "MoneyForward": "マネーフォワード",
    "Claude Code": "クロードコード",
    "GitHub Actions": "ギットハブのアクションズ",
    "MD Workstation": "エムディーワークステーション",
    "actionwithvoice": "アクションウィズボイス",
    "Anthropic": "アンソロピック",
    "Claude": "クロード",
    "GitHub": "ギットハブ",
    "Cmd": "コマンド",
    "Mac": "マック",
    "iPhone": "アイフォーン",
    "Whisper": "ウィスパー",
    "OpenAI": "オープンエーアイ",
    "Cloudflare": "クラウドフレア",
    "Google": "グーグル",
    "LINE": "ライン",
    "PayPay": "ペイペイ",
    "ZOZOTOWN": "ゾゾタウン",
    "Ray": "レイ",
    "Inbox": "インボックス",
    "Notes": "ノート",
    "Projects": "プロジェクト",
    "Data": "データ",
    "OK": "オッケー",
    "git": "ギット",
    "push": "プッシュ",
}
LETTERS = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", [
    "エー", "ビー", "シー", "ディー", "イー", "エフ", "ジー", "エイチ", "アイ", "ジェー", "ケー", "エル", "エム",
    "エヌ", "オー", "ピー", "キュー", "アール", "エス", "ティー", "ユー", "ブイ", "ダブリュー", "エックス", "ワイ", "ゼット",
]))
UNITS = [("kcal", "キロカロリー"), ("kg", "キロ"), ("km", "キロメートル"), ("cm", "センチ"), ("mm", "ミリ"),
         ("GB", "ギガバイト"), ("MB", "メガバイト"), ("KB", "キロバイト"), ("%", "パーセント"), ("％", "パーセント")]

_NOT_LATIN_BEFORE = r"(?<![A-Za-z])"
_NOT_LATIN_AFTER = r"(?![A-Za-z])"
_READING_RES = [
    (re.compile(_NOT_LATIN_BEFORE + re.escape(k) + _NOT_LATIN_AFTER, re.I if k.islower() else 0), v)
    for k, v in sorted(READINGS.items(), key=lambda kv: -len(kv[0]))
]
_FILE = re.compile(r"[A-Za-z0-9_\-/]+\.(?:json|md|py|csv|toml|js|ts|html|txt|ya?ml|png|jpe?g|b64)" + _NOT_LATIN_AFTER, re.I)
_URL = re.compile(r"https?://\S+")
_ACRONYM = re.compile(_NOT_LATIN_BEFORE + r"[A-Z]{1,4}" + _NOT_LATIN_AFTER)
_LATIN = re.compile(r"[A-Za-z]")
_SENT_END = "。？"
_SPLIT = re.compile(r"(?<=[。？])")


def to_japanese(text: str) -> str:
    """英字・記号を、読み上げてよい日本語の形にする"""
    t = _URL.sub("リンク", text)
    t = _FILE.sub("ファイル", t)
    t = re.sub(r"[¥￥]\s*(\d[\d,]*)", r"\1円", t)
    for unit, word in UNITS:
        t = re.sub(r"(\d)\s*" + re.escape(unit) + _NOT_LATIN_AFTER, r"\1" + word, t)
    t = re.sub(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)", r"\1年\2月\3日", t)
    t = re.sub(r"(\d{1,2}):00(?!\d)", r"\1時", t)
    t = re.sub(r"(\d{1,2}):(\d{2})(?!\d)", r"\1時\2分", t)
    t = re.sub(r"(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)", r"\1月\2日", t)
    t = re.sub(r"(\d)\s*[〜~～-]\s*(\d)", r"\1から\2", t)
    for pat, word in _READING_RES:
        t = pat.sub(word, t)
    t = _ACRONYM.sub(lambda m: "".join(LETTERS[c] for c in m.group()), t)
    # 記号：中身は残してかっこだけ消す。区切りの記号は読点に。文の終わりは句点・疑問符にそろえる
    t = re.sub(r"[「」『』（）()\[\]【】〈〉《》<>\"'“”‘’`*_#|>]", "", t)
    t = re.sub(r"[!！]", "。", t)
    t = t.replace("?", "？")
    t = re.sub(r"(?<!\d)[.．](?!\d)", "。", t)
    t = re.sub(r"[:：;；/／→⇒・…〜~～=＝+＋&＆@＠]", "、", t)
    t = re.sub(r"\s*、\s*", "、", t)
    t = re.sub(r"、+", "、", t)
    t = re.sub(r"、([。？])", r"\1", t)
    t = re.sub(r"([。？])、", r"\1", t)
    t = re.sub(r"。+", "。", t)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"(?<=[^\x00-\x7f]) | (?=[^\x00-\x7f])", "", t)  # 日本語のとなりの空白
    return t.strip(" 、")


def has_latin(text: str) -> bool:
    return bool(_LATIN.search(text))


def join(parts) -> str:
    """文をつなぐ。終わりが句点でなければ句点を足し、空白は入れない"""
    out = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if out and out[-1] not in _SENT_END:
            out += "。"
        out += p
    return out


def sentences(text: str) -> list[str]:
    return [s for s in (x.strip() for x in _SPLIT.split(text)) if s]


class Budget:
    """1つの返事で話してよい文の数。超えた分は rest にためる"""

    def __init__(self, limit: int = MAX_SENTENCES):
        self.left = limit
        self.rest: list[str] = []
        self.opened = False

    @property
    def overflow(self) -> bool:
        return bool(self.rest)

    def take(self, text: str, opener_ok: bool = True) -> str:
        s = sentences(text)
        # 作業の前の「調べます」のような短いひとことは、文の数に数えない（最初の1回だけ）
        if opener_ok and not self.opened and len(s) == 1 and len(s[0]) <= OPENER_MAX:
            self.opened = True
            return s[0]
        self.opened = True
        keep = s[: max(self.left, 0)]
        self.rest += s[len(keep):]
        self.left -= len(keep)
        return "".join(keep)
