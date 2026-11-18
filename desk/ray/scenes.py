"""返事を「場面」に分ける。

Claude の返事の中で、行の頭が「@画面」の行は画面の指示（種類と JSON）、
それ以外の行は読み上げる文。指示の行から次の指示の行までが1つの場面になる。

    @画面 calendar {"range": "week"}
    今週は予定が4つあります。
    @画面 calendar {"range": "week", "highlight": ["2026-11-18 19:00"]}
    火曜の19時から飲み会です。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import guard

DIRECTIVE_RE = re.compile(r"^\s*[@＠](?:画面|screen)\s+([^\s{]+)\s*(\{.*\})?\s*$", re.I)


@dataclass
class Scene:
    speech: str = ""
    kind: str | None = None
    params: dict = field(default_factory=dict)
    visual: dict | None = None  # visuals.build の結果


def has_directive(text: str) -> bool:
    return any(DIRECTIVE_RE.match(line) for line in text.splitlines())


def parse(text: str) -> list[Scene]:
    scenes: list[Scene] = []
    cur = Scene()
    for line in text.splitlines():
        m = DIRECTIVE_RE.match(line)
        if m:
            if cur.speech.strip() or cur.kind:
                scenes.append(cur)
            try:
                params = json.loads(m[2]) if m[2] else {}
            except json.JSONDecodeError:
                params = {}
            cur = Scene(kind=m[1], params=params if isinstance(params, dict) else {})
        elif line.strip():
            cur.speech = guard.join([cur.speech, line])
    if cur.speech.strip() or cur.kind:
        scenes.append(cur)
    return scenes


def strip_directives(text: str) -> str:
    """ログや読み上げ用に、指示の行を消した文"""
    return guard.join(l for l in text.splitlines() if l.strip() and not DIRECTIVE_RE.match(l))
