"""Workstation の図解を Ray の画面で使う。

- 描く部分：Workstation の src/renderer/diagram-draw.js（画面なしでも動く）を、そのまま ray/ui/vendor/ に写して使う。
  図のアイコン（Lucide）は src/renderer/icons.js から、図で使うものだけを ray/ui/vendor/diagram-icons.js に抜き出す。
  Mermaid（src/renderer/vendor/mermaid.min.js）も写す（図のライブラリをネットから読まずに済む）。
  写し直すとき：python -m ray --sync-workstation ~/WORKSTATION
- 図の中身：Workstation の図のファイル（名前.diagram.json ＝マウスで描く図 ／ 名前.mmd ＝ Mermaid）を、
  Cmd の中から名前か場所で探して読む（Workstation は新しい図を Cmd の 図解/日付/ に置く）。
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

VENDOR = Path(__file__).resolve().parent / "ui" / "vendor"
DRAW_SRC = Path("src/renderer/diagram-draw.js")
ICONS_SRC = Path("src/renderer/icons.js")
# Mermaid も Workstation が同梱しているものを使う（Workstation の .mmd と同じ見た目になり、ネットから読まずに済む）
COPY = {Path("src/renderer/vendor/mermaid.min.js"): "mermaid.min.js", Path("src/renderer/vendor/LICENSE-mermaid"): "LICENSE-mermaid"}

# 図の「アイコンつきの箱」で選べる名前（Workstation の docs と同じ）と、画像の代わりに出すもの
ICON_NAMES = ["user", "users", "globe", "smartphone", "monitor", "network", "server", "container", "zap", "webhook",
              "database", "hard-drive", "message-square", "mail", "cloud", "shield", "lock", "cpu", "router", "boxes",
              "box", "image"]

# 読まないフォルダ（Workstation の SKIP_DIR と同じ考え）
SKIP_DIR = re.compile(r"^(\.|node_modules$|target$|build$|dist$|out$|venv$|__pycache__$)")
IS_FILE = re.compile(r"\.diagram\.json$|\.mmd$", re.I)
MM_TAGS = re.compile(r"^[ \t]*%%[ \t]*tags[ \t]*:.*(\r?\n|$)", re.I)
EXT_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp"}
MAX_BYTES = 2_000_000      # 図のファイル1つ（画像を除く）
MAX_ASSETS = 4_000_000     # 画像を合わせた大きさ（画面へ送る分）
MAX_DEPTH = 6


# ---------------------------------------------------------------- 写す（Workstation → Ray）

def sync(ws_dir: str | Path, out: Path = VENDOR) -> list[str]:
    """Workstation のチェックアウトから、描く部分とアイコンを写す。書いたファイルの名前を返す"""
    ws = Path(ws_dir).expanduser().resolve()
    draw, icons = ws / DRAW_SRC, ws / ICONS_SRC
    if not draw.exists() or not icons.exists():
        raise FileNotFoundError(f"Workstation のファイルが見つかりません：{draw} / {icons}")
    m = re.search(r"window\.ICONS = (\{.*?\});\n", icons.read_text(encoding="utf-8"), re.S)
    if not m:
        raise ValueError("icons.js の形が変わっていて、アイコンを読めませんでした")
    all_icons = json.loads(m[1])
    picked = {k: all_icons[k] for k in ICON_NAMES if k in all_icons}
    rev = _rev(ws)
    out.mkdir(parents=True, exist_ok=True)
    head = f"// Workstation（{rev}）の {DRAW_SRC.as_posix()} の写し。直すときは Workstation 側を直して、python -m ray --sync-workstation で写し直す\n"
    (out / "diagram-draw.js").write_text(head + draw.read_text(encoding="utf-8"), encoding="utf-8")
    (out / "diagram-icons.js").write_text(
        f"// Lucide アイコン（https://lucide.dev ／ ISC License）。Workstation（{rev}）の icons.js から、図で使うものだけ\n"
        f"window.ICONS = Object.assign(window.ICONS || {{}}, {json.dumps(picked, ensure_ascii=False)});\n", encoding="utf-8")
    copied = []
    for src, name in COPY.items():
        if (ws / src).exists():
            shutil.copyfile(ws / src, out / name)
            copied.append(name)
    return ["diagram-draw.js", "diagram-icons.js", *copied]


def _rev(ws: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(ws), "log", "-1", "--format=%h %cs", "--", str(DRAW_SRC), str(ICONS_SRC)],
                           capture_output=True, text=True, timeout=10)
        return r.stdout.strip() or "版は不明"
    except Exception:  # noqa: BLE001
        return "版は不明"


# ---------------------------------------------------------------- 探す・読む（Cmd の中の図のファイル）

def title_of(f: Path) -> str:
    return re.sub(r"\.diagram\.json$|\.mmd$", "", f.name, flags=re.I)


def list_files(root: Path) -> list[Path]:
    """Cmd の中の図のファイル（新しい順）"""
    out: list[Path] = []
    root = Path(root)
    if not root.is_dir():
        return out
    base = len(root.parts)
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if not SKIP_DIR.match(x) and not x.endswith(".assets")] if len(Path(d).parts) - base < MAX_DEPTH else []
        out += [Path(d) / f for f in files if IS_FILE.search(f) and not f.startswith(".")]
    return sorted(out, key=lambda f: f.stat().st_mtime, reverse=True)


def _norm(s: str) -> str:
    return re.sub(r"[\s　_・\-]", "", s).lower()


def find(root: Path, name: str = "", path: str = "") -> Path | None:
    """場所（Cmd からの相対）か名前で図のファイルを探す。名前は「同じ」→「含む」の順。Cmd の外は見ない"""
    root = Path(root).resolve()
    if path:
        f = (root / path).resolve()
        if f.is_relative_to(root) and f.is_file() and IS_FILE.search(f.name):
            return f
        name = name or title_of(Path(path))
    if not name:
        return None
    files, key = list_files(root), _norm(name)
    for f in files:
        if _norm(title_of(f)) == key:
            return f
    for f in files:
        t = _norm(title_of(f))
        if key in t or (len(t) >= 2 and t in key):
            return f
    return None


def load(f: Path) -> dict:
    """図のファイル → 画面へ送る中身（マウスで描く図は nodes・edges・assets、Mermaid は code）"""
    if f.stat().st_size > MAX_BYTES:
        raise ValueError(f"図のファイルが大きすぎます：{f.name}")
    text = f.read_text(encoding="utf-8")
    if f.name.lower().endswith(".mmd"):
        return {"code": MM_TAGS.sub("", text, count=1).strip()}
    try:
        raw = json.loads(text)
    except ValueError as e:
        raise ValueError(f"図のファイルを読めませんでした（JSON の書き方）：{f.name}") from e
    if not isinstance(raw, dict):
        raise ValueError(f"図のファイルの形が違います：{f.name}")
    d = {"nodes": raw.get("nodes") or [], "edges": raw.get("edges") or []}
    used = {n.get("src") for n in d["nodes"] if isinstance(n, dict) and (n.get("t") or n.get("type")) == "image"}
    assets, total = {}, 0
    for k, rel in (raw.get("assets") or {}).items():
        if k not in used or not isinstance(rel, str):
            continue
        if rel.startswith("data:image/"):
            assets[k], total = rel, total + len(rel)
            continue
        a = (f.parent / rel).resolve()
        mime = EXT_MIME.get(a.suffix.lower())
        if not mime or not a.is_relative_to(f.parent.resolve()) or not a.is_file():
            continue
        if total + a.stat().st_size * 4 // 3 > MAX_ASSETS:
            continue  # 大きすぎる画像は灰色の箱のまま
        s = f"data:{mime};base64," + base64.b64encode(a.read_bytes()).decode()
        assets[k], total = s, total + len(s)
    if assets:
        d["assets"] = assets
    return d


def resolve(root: Path, params: dict) -> dict:
    """diagram の場面の中身。file / name があれば Cmd の図を読み、Claude が書いた中身（title・highlight など）と合わせる"""
    p = dict(params)
    want_path, want_name = str(p.pop("file", "") or p.pop("path", "") or ""), str(p.pop("name", "") or "")
    if not want_path and not want_name:
        return p
    f = find(root, want_name, want_path)
    if not f:
        return {**p, "error": f"図が見つかりませんでした：{want_name or want_path}"}
    got = load(f)
    return {**got, **{k: v for k, v in p.items() if k not in ("code", "nodes", "edges", "assets")},
            "title": p.get("title") or title_of(f), "source": f.relative_to(Path(root).resolve()).as_posix()}

