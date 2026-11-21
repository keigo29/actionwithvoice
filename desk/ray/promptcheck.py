"""プロンプトの効率を確かめる：Cmd の Data/ray/ に残した記録から、送った量・かかった時間・無駄の目安をまとめる

    python -m ray --prompt-report            # 直近7日
    python -m ray --prompt-report --days 30

見るもの
- Claude：毎回読む量（入力トークン。キャッシュに書いた分・読んだ分）、往復の回数、使った道具、時間
- Gemini：入力・出力のトークン
- 言い換え（ローカル 7B）：かかった時間、失敗の割合、減った字数、Claude の入力に対する割合
最後に「気になるところ」として、決めた目安を超えたものと、その対策を出す
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))

# 目安（これを超えたら「気になるところ」に出す）
SLOW_REWRITE_MS = 3000       # 言い換えの待ち
REWRITE_FAIL_RATE = 0.2      # 言い換えの失敗の割合
SMALL_SAVING = 0.02          # 言い換えで減る量が、Claude の入力のこの割合より小さい
MANY_TURNS = 3               # Claude の往復の回数（道具を使うたびに、全部を読み直す）
CACHE_WRITE_SHARE = 0.4      # 入力のうち、キャッシュを作り直した分の割合
BLOWUP = 3                   # 言い換えた依頼が、元の何倍より長いか
CHARS_PER_TOKEN = 1.0        # 日本語はおよそ1字1トークン（目安）


def load(cfg: dict, days: int = 7, now: datetime | None = None) -> list[dict]:
    folder = Path(cfg["cmd_dir"]) / cfg["log"]["dir"]
    since = (now or datetime.now(JST)) - timedelta(days=days)
    rows = []
    for f in sorted(folder.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            when = datetime.fromisoformat(d["datetime"])
        except (OSError, ValueError, KeyError):
            continue
        if when >= since:
            rows.append(d)
    return rows


def _avg(xs) -> float:
    xs = [x for x in xs if isinstance(x, (int, float))]
    return sum(xs) / len(xs) if xs else 0.0


def _total_in(u: dict) -> int:
    return u.get("input", 0) + u.get("cache_write", 0) + u.get("cache_read", 0)


def analyze(rows: list[dict]) -> dict:
    claude = [d["usage"] for d in rows if d.get("brain") == "claude" and isinstance(d.get("usage"), dict)]
    gemini = [d["usage"] for d in rows if d.get("brain") not in ("claude", "local", "canned")
              and isinstance(d.get("usage"), dict)]
    rws = [d["rewrite"] for d in rows if isinstance(d.get("rewrite"), dict)]
    llm = [r for r in rws if "ms" in r]  # 7B を呼んだもの
    applied = [r for r in llm if r.get("applied")]
    out = {
        "records": len(rows),
        "brains": Counter(d.get("brain") or "?" for d in rows),
        "claude": {
            "n": len(claude),
            "input": _avg(_total_in(u) for u in claude),
            "cache_write": _avg(u.get("cache_write") for u in claude),
            "cache_read": _avg(u.get("cache_read") for u in claude),
            "output": _avg(u.get("output") for u in claude),
            "turns": _avg(u.get("turns") for u in claude),
            "ms": _avg(u.get("wall_ms") or u.get("ms") for u in claude),
            "prompt_chars": _avg(u.get("prompt_chars") for u in claude),
            "resumed": sum(1 for u in claude if u.get("resumed")),
            "min_input": min((_total_in(u) for u in claude if u.get("turns") == 1), default=0),
            "tools": Counter(t for u in claude for t in u.get("tools", [])),
            "cost_usd": sum(u.get("cost_usd") or 0 for u in claude),
        },
        "gemini": {"n": len(gemini), "input": _avg(u.get("input") for u in gemini),
                   "output": _avg(u.get("output") for u in gemini), "system_chars": _avg(u.get("system_chars") for u in gemini)},
        "rewrite": {
            "checked": len(rws),
            "reasons": Counter(r.get("reason") for r in rws),
            "llm_calls": len(llm),
            "llm_ms": _avg(r["ms"] for r in llm),
            "llm_wait_s": sum(r["ms"] for r in llm) / 1000,
            "errors": Counter(r["error"] for r in llm if r.get("error")),
            "applied": len(applied),
            "saved_chars": _avg(r.get("before_chars", 0) - r.get("after_chars", 0) for r in applied),
            "blowups": sum(1 for r in rws if r.get("applied") and r.get("after_chars", 0) > BLOWUP * max(1, r.get("before_chars", 0))),
            "refs_only": sum(1 for r in rws if str(r.get("reason", "")).endswith("+refs")),
        },
    }
    out["findings"] = findings(out)
    return out


def findings(a: dict) -> list[str]:
    f = []
    c, r = a["claude"], a["rewrite"]
    if r["llm_calls"]:
        fail = sum(r["errors"].values()) / r["llm_calls"]
        if r["llm_ms"] > SLOW_REWRITE_MS:
            f.append(f"言い換え（7B）の待ちが平均{r['llm_ms'] / 1000:.1f}秒。そのぶん喋り出しが遅れる"
                     f"（合わせて{r['llm_wait_s']:.0f}秒）。→ [rewrite] llm_reasons を絞るか、min_chars を上げる")
        if fail > REWRITE_FAIL_RATE:
            f.append(f"言い換えの{fail:.0%}が失敗（{dict(r['errors'])}）。失敗した分は待っただけになる。"
                     "→ timeout と、7B に回す条件を見直す")
        if c["input"] and r["applied"]:
            share = max(0.0, r["saved_chars"]) * CHARS_PER_TOKEN / c["input"]
            if share < SMALL_SAVING:
                f.append(f"言い換えで減るのは平均{r['saved_chars']:.0f}字。Claude が毎回読む約{c['input']:,.0f}トークンの"
                         f"{share:.1%}で、効率にはほとんど効かない。→ 7B は聞き取りの崩れを直す用途に絞る")
    if r["blowups"]:
        f.append(f"言い換えた依頼が元の{BLOWUP}倍より長くなったものが{r['blowups']}件。"
                 "テンプレや添える文が長すぎないか見る（短い質問ほど膨らみやすい）")
    if c["n"]:
        if c["turns"] > MANY_TURNS:
            top = "、".join(f"{k}×{v}" for k, v in c["tools"].most_common(3))
            f.append(f"Claude の往復が平均{c['turns']:.1f}回（道具：{top}）。往復のたびに全部を読み直す。"
                     "→ 先に見るファイル（REF_RULES）を足して、探す回数を減らす")
        if c["input"] and c["cache_write"] / c["input"] > CACHE_WRITE_SHARE:
            f.append(f"入力の{c['cache_write'] / c['input']:.0%}がキャッシュの作り直し（読むより高い）。"
                     "新しいセッションが多いか、間があいている。→ 会話の続きは同じセッションで")
        if c["min_input"] and c["prompt_chars"] and c["prompt_chars"] * CHARS_PER_TOKEN < c["min_input"] * 0.05:
            f.append(f"Claude の入力のほとんどは、毎回つく決まった部分（いちばん少ない回でも約{c['min_input']:,}トークン）。"
                     f"送る文（平均{c['prompt_chars']:.0f}字）を削るより、決まった部分（Ray の指示・CLAUDE.md・道具の説明）を減らすほうが効く")
    if not c["n"]:
        f.append("Claude の使った量の記録がまだありません（この仕組みを入れたあとの会話から残ります）")
    return f


def report(a: dict, days: int) -> str:
    c, g, r = a["claude"], a["gemini"], a["rewrite"]
    lines = [f"■ プロンプトの効率（直近{days}日・{a['records']}件）",
             "答えた頭脳：" + "、".join(f"{k} {v}" for k, v in a["brains"].most_common()), ""]
    if c["n"]:
        lines += [f"■ Claude（記録あり {c['n']}件・続きの会話 {c['resumed']}件）",
                  f"  入力 平均{c['input']:,.0f}トークン（キャッシュに書いた {c['cache_write']:,.0f}・読んだ {c['cache_read']:,.0f}）",
                  f"  出力 平均{c['output']:,.0f}トークン／往復 平均{c['turns']:.1f}回／時間 平均{c['ms'] / 1000:.1f}秒",
                  f"  送った文 平均{c['prompt_chars']:.0f}字／いちばん少ない回の入力 {c['min_input']:,}トークン",
                  "  使った道具：" + ("、".join(f"{k}×{v}" for k, v in c["tools"].most_common(6)) or "なし"),
                  f"  API の定価で数えると {c['cost_usd']:.2f}ドル（Pro では請求されない。量の目安）", ""]
    if g["n"]:
        lines += [f"■ Gemini（{g['n']}件）",
                  f"  入力 平均{g['input']:,.0f}トークン／出力 平均{g['output']:,.0f}トークン／指示 平均{g['system_chars']:.0f}字", ""]
    lines += [f"■ 言い換え（Claude 行き {r['checked']}件）",
              "  理由：" + "、".join(f"{k} {v}" for k, v in r["reasons"].most_common()),
              f"  7B を呼んだ {r['llm_calls']}回（平均{r['llm_ms'] / 1000:.1f}秒・合わせて{r['llm_wait_s']:.0f}秒）"
              f"／使えた {r['applied']}回／失敗 {dict(r['errors']) or 'なし'}",
              f"  減った字数 平均{r['saved_chars']:.0f}字／ファイルだけ添えた {r['refs_only']}回", ""]
    lines.append("■ 気になるところ")
    lines += [f"  ・{x}" for x in a["findings"]] or ["  なし"]
    return "\n".join(lines)


def run(cfg: dict, days: int = 7) -> str:
    return report(analyze(load(cfg, days)), days)
