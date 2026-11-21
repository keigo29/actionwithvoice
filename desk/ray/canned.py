"""定番の返事：毎回同じでよい返事（自己紹介など）は、用意しておいたものをそのまま使う。Claude も Gemini も呼ばない

1つの返事が1つのファイル（.md）。頭に「match:」などの設定、「---」の行のあとが返事の本文。
本文は Claude の返事と同じ形で、「@画面 種類 {JSON}」の行で図（Mermaid）やスライドも出せる。

    match: ^(?:自己紹介|紹介して)$
    max_chars: 20
    ---
    @画面 steps {"title": "できること", "steps": ["予定", "調べもの"]}
    私はレイです。

置き場所は2つ。読むたびにファイルを見るので、書きかえたら起動し直さなくても次から変わる
- desk/ray/canned/：Ray に最初から入っている定番（Git に入る）
- Cmd の Data/ray/canned/：「今の答えを定番にして」で保存したもの（持ち主のデータを含みうるので Cmd に置く）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

BUILTIN_DIR = Path(__file__).resolve().parent / "canned"
DEFAULT_MAX = 20  # これより長い発話は、定番に当てない（「自己紹介の文を短くするには」のような話を拾わない）

# 定番を変える・作る話は、定番で答えずにいつもの振り分けへ（「自己紹介を直して」など）
EDIT_RE = re.compile(r"変え|変更|直|編集|修正|書き換|消|削除|ソース|コード|作り直|追加|短く|長く")
# 直前の答えを定番として保存する
SAVE_RE = re.compile(r"^(?:今の|いまの|さっきの)?(?:答え|返事|説明)?を?(?:定番|テンプレ)(?:に|として)"
                     r"(?:して|する|保存|登録|しておいて|とっておいて|覚えて)")
_PUNCT = re.compile(r"[、。，．,.？?！!「」\s]")


@dataclass
class Canned:
    name: str
    pattern: re.Pattern
    max_chars: int
    body: str


def norm(text: str) -> str:
    return _PUNCT.sub("", text.strip())


def parse(text: str, name: str = "") -> Canned | None:
    head, sep, body = text.partition("\n---\n")
    if not sep or not body.strip():
        return None
    meta = {}
    for line in head.splitlines():
        k, _, v = line.partition(":")
        if v:
            meta[k.strip()] = v.strip()
    if not meta.get("match"):
        return None
    try:
        pattern = re.compile(meta["match"])
    except re.error:
        print(f"定番の返事「{name}」の match が読めません：{meta['match']}")
        return None
    try:
        max_chars = int(meta.get("max_chars", DEFAULT_MAX))
    except ValueError:
        max_chars = DEFAULT_MAX
    return Canned(name=name, pattern=pattern, max_chars=max_chars, body=body.strip())


def load(dirs) -> list[Canned]:
    found = []
    for d in dirs:
        d = Path(d)
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.md")):
            try:
                c = parse(f.read_text(encoding="utf-8"), f.stem)
            except OSError:
                continue
            if c:
                found.append(c)
    return found


def dirs_for(cfg: dict) -> list[Path]:
    return [BUILTIN_DIR, saved_dir(cfg)]


def saved_dir(cfg: dict) -> Path:
    return Path(cfg["cmd_dir"]) / "Data" / "ray" / "canned"


def find(text: str, dirs) -> Canned | None:
    t = norm(text)
    if not t or EDIT_RE.search(t):
        return None
    for c in load(dirs):
        if len(t) <= c.max_chars and c.pattern.search(t):
            return c
    return None


def is_save(text: str) -> bool:
    return bool(SAVE_RE.search(norm(text)))


def save(question: str, body: str, d: Path) -> Path:
    """直前の質問と同じ言い方で聞かれたら、この答えを返す定番として書き出す"""
    q = norm(question)
    name = re.sub(r"[\\/:*?\"<>|]", "", q)[:20] or "定番"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{name}.md"
    path.write_text(f"match: ^{re.escape(q)}$\nmax_chars: {max(DEFAULT_MAX, len(q))}\n---\n{body.strip()}\n",
                    encoding="utf-8")
    return path
