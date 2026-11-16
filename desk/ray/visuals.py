"""画面に出す絵のデータを作る。

返事の中の「@画面 種類 {JSON}」の種類ごとに、Cmd のファイルなどから絵のデータを組み立てる。
Cmd から作れるもの（カレンダー・やること・習慣など）は Ray が自分で集め、
Claude が考えて決めるもの（表・手順・ウェブの引用など）は JSON をそのまま使う。
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import subprocess
import urllib.request
from pathlib import Path

SKIP_DIRS = {".git", "Data", "Templates", "node_modules", ".obsidian", "scripts", ".github"}
WEEK = "月火水木金土日"

EVENT_RE = re.compile(r"^\s*-\s+(\d{1,2}:\d{2})(?:\s*[-〜~]\s*(\d{1,2}:\d{2}))?\s+(.+)$")
TODO_RE = re.compile(r"^\s*-\s+\[([ xX])\]\s+(.+)$")
DUE_RE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
START_RE = re.compile(r"🛫\s*(\d{4}-\d{2}-\d{2})")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")
DATE_HEAD_RE = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2})\s*$")
DAILY_RE = re.compile(r"^(\d{4})/(\d{2})/(\d{2})/")
WEIGHT_RE = re.compile(r"^\s*-\s+体重\s+([\d.]+)\s*kg")
MOOD_RE = re.compile(r"^\s*-\s+気分\s+([1-5])\s*/\s*5\s*(.*)$")
REST_RE = re.compile(r"^\s*-\s+(\d{4}-\d{2}-\d{2})(?:\s*[〜~]\s*(\d{4}-\d{2}-\d{2}))?\s*(.*)$")

WEATHER_CODES = {
    0: "晴れ", 1: "晴れ", 2: "くもり時々晴れ", 3: "くもり", 45: "霧", 48: "霧",
    51: "霧雨", 53: "霧雨", 55: "霧雨", 61: "小雨", 63: "雨", 65: "大雨",
    71: "小雪", 73: "雪", 75: "大雪", 80: "にわか雨", 81: "にわか雨", 82: "強いにわか雨",
    95: "雷雨", 96: "雷雨", 99: "雷雨",
}


# ---------------------------------------------------------------- 共通

def today() -> dt.date:
    return dt.date.today()


def to_date(s, default: dt.date | None = None) -> dt.date:
    if isinstance(s, dt.date):
        return s
    if s in (None, "", "today", "今日"):
        return default or today()
    if s in ("tomorrow", "明日"):
        return (default or today()) + dt.timedelta(days=1)
    return dt.date.fromisoformat(str(s)[:10])


def md_files(root: Path):
    for p in sorted(root.rglob("*.md")):
        rel = p.relative_to(root)
        if rel.parts and rel.parts[0] in SKIP_DIRS:
            continue
        yield rel.as_posix(), p


def read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def daily_date(rel: str) -> dt.date | None:
    m = DAILY_RE.match(rel)
    if not m:
        return None
    try:
        return dt.date(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def frontmatter(text: str) -> dict:
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    out = {}
    for line in text[3:end].splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def clean_title(s: str) -> str:
    s = DUE_RE.sub("", s)
    s = START_RE.sub("", s)
    s = s.replace("#節目", "")
    s = LINK_RE.sub(lambda m: m[1], s)
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- 1 カレンダー

def collect_events(root: Path) -> list[dict]:
    """予定（デイリーの ## 予定、## YYYY-MM-DD の下）と、期限（📅）・節目を集める"""
    events: list[dict] = []
    for rel, p in md_files(root):
        text = read(p)
        day = daily_date(rel)
        section_date: dt.date | None = None
        in_plan = False
        for line in text.splitlines():
            if line.startswith("## "):
                m = DATE_HEAD_RE.match(line)
                section_date = dt.date.fromisoformat(m[1]) if m else None
                in_plan = line.strip() == "## 予定"
                continue
            on = section_date or (day if in_plan else None)
            m = EVENT_RE.match(line)
            if on and m and "🎙" not in line:
                events.append({
                    "date": on.isoformat(), "start": m[1].zfill(5), "end": (m[2] or "").zfill(5) if m[2] else "",
                    "title": clean_title(m[3]), "kind": "milestone" if "#節目" in line else "event", "source": rel,
                })
                continue
            t = TODO_RE.match(line)
            if t:
                due = DUE_RE.search(line)
                if due:
                    events.append({
                        "date": due[1], "start": "", "end": "", "title": clean_title(t[2]),
                        "kind": "milestone" if "#節目" in line else "due", "done": t[1] != " ", "source": rel,
                    })
    return events


def calendar(root: Path, params: dict) -> dict:
    rng = params.get("range", "week")
    base = to_date(params.get("date"))
    if rng == "day":
        start = end = base
    elif rng == "month":
        start = base.replace(day=1)
        end = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    else:
        rng = "week"
        start = base - dt.timedelta(days=base.weekday())
        end = start + dt.timedelta(days=6)
    events = [e for e in collect_events(root) if start.isoformat() <= e["date"] <= end.isoformat()]
    events += [e for e in params.get("extra", []) if isinstance(e, dict) and e.get("date")]
    events.sort(key=lambda e: (e["date"], e.get("start") or "99:99"))
    days = [(start + dt.timedelta(days=i)) for i in range((end - start).days + 1)]
    return {
        "range": rng, "start": start.isoformat(), "end": end.isoformat(), "today": today().isoformat(),
        "days": [{"date": d.isoformat(), "dow": WEEK[d.weekday()]} for d in days],
        "events": events, "highlight": params.get("highlight", []),
        "title": params.get("title") or {"day": f"{base.month}月{base.day}日（{WEEK[base.weekday()]}）",
                                          "week": f"{start.month}/{start.day} 〜 {end.month}/{end.day}",
                                          "month": f"{start.year}年{start.month}月"}[rng],
    }


# ---------------------------------------------------------------- 3 ノート

def note(root: Path, params: dict) -> dict:
    rel = str(params.get("path", "")).lstrip("/")
    p = (root / rel).resolve()
    if not rel or root.resolve() not in p.parents or not p.exists():
        return {"error": f"ノートが見つかりません：{rel}"}
    lines = read(p).splitlines()
    quotes = params.get("quote") or params.get("quotes") or []
    if isinstance(quotes, str):
        quotes = [quotes]
    norm = lambda s: re.sub(r"\s+", "", s)
    hits = [i for i, line in enumerate(lines) if any(norm(q) and norm(q) in norm(line) for q in quotes)]
    if params.get("line"):
        hits.append(int(params["line"]) - 1)
    ctx = int(params.get("context", 3))
    if hits:
        lo, hi = max(0, min(hits) - ctx), min(len(lines), max(hits) + ctx + 1)
    else:
        lo, hi = 0, min(len(lines), 14)
    title = next((l[2:] for l in lines if l.startswith("# ")), p.stem)
    return {
        "path": rel, "title": title,
        "lines": [{"n": i + 1, "text": lines[i], "hit": i in hits} for i in range(lo, hi)],
    }


# ---------------------------------------------------------------- 4 やること

def collect_todos(root: Path) -> list[dict]:
    projects = {p.stem for p in (root / "Projects").glob("*.md")} if (root / "Projects").exists() else set()
    out = []
    for rel, p in md_files(root):
        own = Path(rel).stem if rel.startswith("Projects/") else None
        for n, line in enumerate(read(p).splitlines(), 1):
            m = TODO_RE.match(line)
            if not m:
                continue
            due, start = DUE_RE.search(line), START_RE.search(line)
            links = [l for l in LINK_RE.findall(line) if l in projects]
            out.append({
                "title": clean_title(LINK_RE.sub(lambda x: "" if x[1] in projects else x[0], m[2])), "done": m[1] != " ", "due": due[1] if due else "",
                "start": start[1] if start else "", "project": own or (links[0] if links else ""),
                "milestone": "#節目" in line, "source": rel, "line": n,
            })
    return out


def todos(root: Path, params: dict) -> dict:
    items = collect_todos(root)
    t = today().isoformat()
    project = params.get("project")
    if project:
        items = [i for i in items if i["project"] == project]
    shown = [i for i in items if not i["done"] or params.get("include_done")]
    if params.get("due_within"):
        limit = (today() + dt.timedelta(days=int(params["due_within"]))).isoformat()
        shown = [i for i in shown if i["due"] and i["due"] <= limit]
    for i in shown:
        i["overdue"] = bool(i["due"]) and i["due"] < t and not i["done"]
    shown.sort(key=lambda i: (i["done"], i["due"] or "9999", i["source"], i["line"]))
    shown = shown[: int(params.get("limit", 12))]
    progress = {}
    for i in items:
        if i["project"]:
            d = progress.setdefault(i["project"], {"name": i["project"], "done": 0, "total": 0})
            d["total"] += 1
            d["done"] += i["done"]
    return {"today": t, "items": shown, "projects": sorted(progress.values(), key=lambda d: d["name"]),
            "highlight": params.get("highlight", []), "title": params.get("title") or (project or "やること")}


# ---------------------------------------------------------------- 5 習慣

def parse_habits(root: Path) -> tuple[list[dict], list[tuple[str, str, str]]]:
    text = read(root / "Notes" / "毎日確認.md")
    habits, rests, cols, in_rest = [], [], None, False
    for line in text.splitlines():
        if line.startswith("## "):
            in_rest = line.strip() == "## 休みの日"
            continue
        if in_rest:
            m = REST_RE.match(line)
            if m:
                rests.append((m[1], m[2] or m[1], m[3].strip()))
            continue
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if all(re.fullmatch(r":?-+:?", c) for c in cells):
                continue
            if cols is None:
                cols = cells
                continue
            row = dict(zip(cols, cells))
            if row.get("プロパティ"):
                habits.append({"name": row.get("項目", ""), "prop": row["プロパティ"], "color": row.get("色", "#4a5cff"),
                               "days": row.get("曜日", "毎日"), "rest": row.get("休みの日", "続ける"),
                               "since": row.get("始めた日", "")})
    return habits, rests


def habits(root: Path, params: dict) -> dict:
    hs, rests = parse_habits(root)
    n = {"week": 7, "month": 30}.get(params.get("range", "week"), int(params.get("days", 7)))
    end = to_date(params.get("date"))
    days = [end - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]
    lookback = [end - dt.timedelta(days=i) for i in range(120)]
    props: dict[str, dict] = {}
    for d in set(days) | set(lookback):
        props[d.isoformat()] = frontmatter(read(root / f"{d:%Y/%m/%d}" / "デイリー.md"))

    def rest_day(d: dt.date) -> str:
        for a, b, label in rests:
            if a <= d.isoformat() <= b:
                return label or "休み"
        return ""

    def scheduled(h: dict, d: dt.date) -> bool:
        if h["since"] and d.isoformat() < h["since"]:
            return False
        if h["rest"] == "休む" and rest_day(d):
            return False
        return h["days"] == "毎日" or WEEK[d.weekday()] in h["days"]

    rows = []
    for h in hs:
        cells = []
        for d in days:
            v = props[d.isoformat()].get(h["prop"])
            state = "off" if not scheduled(h, d) else {"1": "done", "0": "miss"}.get(v, "none")
            cells.append({"date": d.isoformat(), "state": state})
        streak = 0
        for i, d in enumerate(lookback):
            if not scheduled(h, d):
                continue
            v = props[d.isoformat()].get(h["prop"])
            if v == "1":
                streak += 1
            elif i == 0:
                continue  # 今日はまだつけていないだけかもしれない
            else:
                break
        done = sum(c["state"] == "done" for c in cells)
        due = sum(c["state"] in ("done", "miss", "none") for c in cells)
        rows.append({"name": h["name"], "color": h["color"], "cells": cells, "streak": streak, "done": done, "due": due})
    return {"days": [{"date": d.isoformat(), "dow": WEEK[d.weekday()], "rest": rest_day(d)} for d in days],
            "habits": rows, "highlight": params.get("highlight", []), "title": params.get("title") or "毎日確認"}


# ---------------------------------------------------------------- 6 体重・気分

def body(root: Path, params: dict) -> dict:
    n = int(params.get("days", 30))
    end = to_date(params.get("date"))
    start = end - dt.timedelta(days=n - 1)
    weight: dict[str, float] = {}
    mood: dict[str, list[int]] = {}
    for f in sorted((root / "Data" / "health").glob("*.json")):
        try:
            for d, v in json.loads(read(f)).items():
                if isinstance(v, dict) and v.get("weight_kg"):
                    weight[d] = float(v["weight_kg"])
        except (ValueError, AttributeError):
            pass
    for i in range(n):
        d = start + dt.timedelta(days=i)
        for p in (root / f"{d:%Y/%m/%d}").glob("*.md"):
            for line in read(p).splitlines():
                if m := WEIGHT_RE.match(line):
                    weight[d.isoformat()] = float(m[1])
                elif m := MOOD_RE.match(line):
                    mood.setdefault(d.isoformat(), []).append(int(m[1]))
    keep = lambda k: start.isoformat() <= k <= end.isoformat()
    w = [{"date": k, "value": v} for k, v in sorted(weight.items()) if keep(k)]
    md = [{"date": k, "value": round(sum(v) / len(v), 1)} for k, v in sorted(mood.items()) if keep(k)]
    metric = params.get("metric", "both")
    out = {"start": start.isoformat(), "end": end.isoformat(), "series": [],
           "title": params.get("title") or "体重・気分"}
    if metric in ("weight", "both"):
        out["series"].append({"name": "体重", "unit": "kg", "points": w, "target": params.get("target")})
    if metric in ("mood", "both"):
        out["series"].append({"name": "気分", "unit": "/5", "points": md, "min": 1, "max": 5})
    for s in out["series"]:
        pts = s["points"]
        if len(pts) >= 2:
            s["change"] = round(pts[-1]["value"] - pts[0]["value"], 1)
    return out


# ---------------------------------------------------------------- 7 家計

KAKEI_LABELS = {
    "food": "食費", "daily": "日用品", "phone": "通信費", "clothes": "衣服", "hobby": "趣味",
    "rent": "家賃", "utility": "光熱費", "transport": "交通費", "medical": "医療", "social": "交際費",
    "beauty": "美容", "education": "教育", "insurance": "保険", "tax": "税金", "other-ex": "その他",
}

def kakei(root: Path, params: dict) -> dict:
    p = root / "Data" / "kakei.json"
    if not p.exists():
        return {"error": "Data/kakei.json がありません"}
    data = json.loads(read(p))
    month = params.get("month") or today().strftime("%Y-%m")
    txs = [t for t in data.get("txs", []) if str(t.get("date", "")).startswith(month)]
    by_cat: dict[str, int] = {}
    income = expense = 0
    for t in txs:
        amt = int(t.get("amount", 0))
        if t.get("type") == "income":
            income += amt
        elif t.get("type") == "expense":
            expense += amt
            by_cat[t.get("category") or "未分類"] = by_cat.get(t.get("category") or "未分類", 0) + amt
    labels = {**KAKEI_LABELS, **params.get("labels", {})}
    y, m = map(int, month.split("-"))
    last = (dt.date(y, m, 28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
    left = max(0, (last - today()).days + 1) if month == today().strftime("%Y-%m") else 0
    budget = int(params.get("budget") or data.get("settings", {}).get("budget") or 0)
    return {
        "month": month, "income": income, "expense": expense, "budget": budget, "days_left": left,
        "remaining": budget - expense if budget else None,
        "categories": [{"name": labels.get(k, k), "amount": v} for k, v in sorted(by_cat.items(), key=lambda kv: -kv[1])],
        "highlight": params.get("highlight", []), "title": params.get("title") or f"{y}年{m}月の家計",
    }


# ---------------------------------------------------------------- 8 筋トレ

def workout(root: Path, params: dict) -> dict:
    p = root / "Data" / "kintore" / "sets.csv"
    if not p.exists():
        return {"error": "Data/kintore/sets.csv がありません"}
    since = (today() - dt.timedelta(days=int(params.get("days", 90)))).isoformat()
    want = params.get("exercise")
    best: dict[str, dict[str, dict]] = {}
    with p.open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("date", "") < since or r.get("kind") not in ("normal", ""):
                continue
            ex = r.get("exercise", "")
            if want and want not in ex:
                continue
            try:
                w, e1 = float(r.get("weight_kg") or 0), float(r.get("e1rm_kg") or 0)
            except ValueError:
                continue
            d = best.setdefault(ex, {}).setdefault(r["date"], {"date": r["date"], "weight": 0, "e1rm": 0})
            d["weight"], d["e1rm"] = max(d["weight"], w), max(d["e1rm"], e1)
    series = sorted(({"name": ex, "points": sorted(v.values(), key=lambda x: x["date"])} for ex, v in best.items()),
                    key=lambda s: -len(s["points"]))[: int(params.get("limit", 3))]
    return {"series": series, "menu": params.get("menu", []), "title": params.get("title") or "筋トレ",
            "highlight": params.get("highlight", [])}


# ---------------------------------------------------------------- 9 天気

_WEATHER_CACHE: dict[str, tuple[float, str]] = {}


def _get(url: str) -> str:
    """同じ URL は30分のあいだ使い回す（天気の API を呼びすぎない）"""
    import time

    hit = _WEATHER_CACHE.get(url)
    if hit and time.time() - hit[0] < 1800:
        return hit[1]
    body = urllib.request.urlopen(url, timeout=8).read().decode()
    _WEATHER_CACHE[url] = (time.time(), body)
    return body


def weather(cfg: dict, params: dict, fetch=None) -> dict:
    w = cfg.get("weather", {})
    lat, lon, place = w.get("latitude", 35.44), w.get("longitude", 139.64), w.get("place", "横浜")
    day = to_date(params.get("date"))
    url = ("https://api.open-meteo.com/v1/forecast?"
           f"latitude={lat}&longitude={lon}&hourly=temperature_2m,precipitation_probability,weather_code"
           f"&timezone=Asia%2FTokyo&start_date={day}&end_date={day}")
    try:
        raw = (fetch or _get)(url)
        h = json.loads(raw)["hourly"]
    except Exception as e:  # noqa: BLE001 ネットが無くても画面は出す
        return {"error": f"天気を取れませんでした（{e}）", "place": place, "date": day.isoformat()}
    hours = [{"time": t[11:16], "temp": round(tp), "pop": pp or 0, "sky": WEATHER_CODES.get(c, "")}
             for t, tp, pp, c in zip(h["time"], h["temperature_2m"], h["precipitation_probability"], h["weather_code"])]
    every = int(params.get("every", 3))
    temps = [x["temp"] for x in hours]
    return {"place": place, "date": day.isoformat(), "hours": hours[::every] if every > 1 else hours,
            "max": max(temps), "min": min(temps), "pop": max(x["pop"] for x in hours),
            "title": params.get("title") or f"{place}の天気（{day.month}/{day.day}）"}


# ---------------------------------------------------------------- 12 ファイルの変更

def diff(root: Path, params: dict, run=subprocess.run) -> dict:
    ref = params.get("ref", "HEAD")
    repo = Path(params["repo"]).expanduser() if params.get("repo") else root
    if ref == "worktree":
        cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false", "diff", "--no-color"]
        head = "まだコミットしていない変更"
    else:
        if not re.fullmatch(r"[\w./~^-]{1,60}", ref):
            return {"error": "ref の形が正しくありません"}
        cmd = ["git", "-C", str(repo), "-c", "core.quotepath=false", "show", "--no-color", "--format=%h %s", ref]
        head = ""
    try:
        out = run(cmd, capture_output=True, text=True, timeout=15).stdout
        status = run(["git", "-C", str(repo), "status", "--short", "--branch"],
                     capture_output=True, text=True, timeout=15).stdout
    except Exception as e:  # noqa: BLE001
        return {"error": f"git を動かせませんでした（{e}）"}
    lines = out.splitlines()
    if not head and lines:
        head, lines = lines[0], lines[1:]
    files, cur = [], None
    for line in lines:
        if line.startswith("diff --git"):
            cur = {"path": line.split(" b/", 1)[-1], "lines": []}
            files.append(cur)
        elif cur is not None and not line.startswith(("index ", "--- ", "+++ ", "new file", "deleted file")):
            kind = "add" if line.startswith("+") else "del" if line.startswith("-") else "hunk" if line.startswith("@@") else "ctx"
            if len(cur["lines"]) < int(params.get("max_lines", 60)):
                cur["lines"].append({"kind": kind, "text": line})
    return {"head": head, "files": files[: int(params.get("max_files", 5))], "status": status.splitlines()[:10],
            "title": params.get("title") or "ファイルの変更"}


# ---------------------------------------------------------------- 入口

# Claude が書いた中身をそのまま使うもの
PASSTHROUGH = {"web", "sources", "table", "steps", "calc", "confirm", "text", "diagram"}
KINDS = {"calendar", "note", "todos", "habits", "body", "kakei", "workout", "weather", "diff"} | PASSTHROUGH
ALIASES = {"予定": "calendar", "カレンダー": "calendar", "ノート": "note", "やること": "todos", "タスク": "todos",
           "習慣": "habits", "毎日確認": "habits", "体重": "body", "気分": "body", "家計": "kakei",
           "筋トレ": "workout", "天気": "weather", "変更": "diff", "ウェブ": "web", "出典": "sources",
           "表": "table", "比較": "table", "手順": "steps", "計算": "calc", "確認": "confirm",
           "mermaid": "diagram", "マーメイド": "diagram", "図": "diagram", "フロー": "diagram"}


def build(kind: str, params: dict, cfg: dict) -> dict:
    kind = ALIASES.get(kind, kind)
    root = Path(cfg["cmd_dir"]).expanduser()
    params = params if isinstance(params, dict) else {}
    try:
        if kind in PASSTHROUGH:
            data = dict(params)
        elif kind == "weather":
            data = weather(cfg, params)
        elif kind in KINDS:
            data = globals()[kind](root, params)
        else:
            return {"kind": "text", "data": {"error": f"知らない種類です：{kind}"}}
    except Exception as e:  # noqa: BLE001 絵が作れなくても話は続ける
        data = {"error": f"{kind} を作れませんでした（{e}）"}
    return {"kind": kind, "data": data}
