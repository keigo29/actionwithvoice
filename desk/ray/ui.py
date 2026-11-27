"""状態表示：光る円の HTML 1枚と、状態を流す SSE（http://localhost:8765）

POST /notify：n8n などから Ray に話させる（[notify]）。JSON で {"text": "…", "title": "…"}。
text は Claude の返事と同じ形で、「@画面 種類 {JSON}」の行も使える。
合言葉（X-Ray-Token か Authorization: Bearer）が合うときだけ受け付ける。
ブラウザのページから勝手に送られないよう、Content-Type が application/json のものだけ受ける
"""

from __future__ import annotations

import hmac
import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import shots

PAGE = Path(__file__).resolve().parent / "ui" / "index.html"
FONTS = Path(__file__).resolve().parent / "ui" / "fonts"
VENDOR = Path(__file__).resolve().parent / "ui" / "vendor"  # Workstation から写した図を描く部分（ray/wsdiagram.py）
FONT_TYPES = {".otf": "font/otf", ".ttf": "font/ttf", ".woff": "font/woff", ".woff2": "font/woff2"}


class StateHub:
    def __init__(self):
        self.state = {"state": "idle", "text": "", "question": "", "answer": "", "scene": None, "zoom": False}
        self.listeners: list[queue.Queue] = []
        self.lock = threading.Lock()

    def set(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)
            snap = dict(self.state)
            for q in list(self.listeners):
                q.put(snap)

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue()
        with self.lock:
            self.listeners.append(q)
            q.put(dict(self.state))
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


def check_notify(headers, body: bytes, token: str, max_bytes: int = 65536) -> tuple[int, dict]:
    """受け取った知らせを確かめる。(HTTP の番号, 中身 か エラー)"""
    if not token:
        return 403, {"error": "config.toml の [notify] token が空です"}
    given = headers.get("X-Ray-Token") or ""
    auth = headers.get("Authorization") or ""
    if not given and auth.lower().startswith("bearer "):
        given = auth[7:].strip()
    if not hmac.compare_digest(given.encode(), token.encode()):
        return 401, {"error": "合言葉が違います"}
    if not (headers.get("Content-Type") or "").lower().startswith("application/json"):
        return 415, {"error": "Content-Type は application/json にしてください"}
    if len(body) > max_bytes:
        return 413, {"error": f"{max_bytes} バイトまでです"}
    try:
        d = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return 400, {"error": "JSON が読めません"}
    if not isinstance(d, dict) or not str(d.get("text") or "").strip():
        return 400, {"error": "text が空です"}
    return 202, {"text": str(d["text"]).strip(), "title": str(d.get("title") or "").strip()}


def serve(hub: StateHub, port: int, on_notify: Callable[[str, str], int] | None = None,
          notify_cfg: dict | None = None, host: str = "127.0.0.1", notify_only: bool = False) -> ThreadingHTTPServer:
    """状態の画面と知らせの受け口。notify_only なら POST /notify だけ受ける（Mac の外に開くとき用）"""
    notify_cfg = notify_cfg or {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _json(self, code: int, d: dict) -> None:
            body = json.dumps(d, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path.split("?", 1)[0] != "/notify" or not on_notify:
                self.send_error(404)
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            limit = int(notify_cfg.get("max_bytes", 65536))
            body = self.rfile.read(min(n, limit + 1)) if n > 0 else b""
            code, d = check_notify(self.headers, body, notify_cfg.get("token", ""), limit)
            if code != 202:
                self._json(code, d)
                return
            self._json(202, {"ok": True, "queued": on_notify(d["text"], d["title"])})

        def do_GET(self):
            if notify_only:  # 画面には会話の中身が出るので、外からは見せない
                self.send_error(404)
            elif self.path.split("?", 1)[0] == "/":  # 「/?theme=blueprint」のように見た目を選べる
                body = PAGE.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            elif self.path == "/events":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q = hub.subscribe()
                try:
                    while True:
                        try:
                            snap = q.get(timeout=15)
                            self.wfile.write(f"data: {json.dumps(snap, ensure_ascii=False)}\n\n".encode())
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    hub.unsubscribe(q)
            elif self.path.startswith("/fonts/"):
                name = self.path[len("/fonts/"):]
                f = FONTS / name
                kind = FONT_TYPES.get(Path(name).suffix.lower())
                if "/" in name or ".." in name or not kind or not f.exists():
                    self.send_error(404)
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/vendor/"):
                name = self.path[len("/vendor/"):].split("?", 1)[0]
                f = VENDOR / name
                if "/" in name or ".." in name or not name.endswith(".js") or not f.exists():
                    self.send_error(404)
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "max-age=86400")  # Mermaid は大きいので、毎回は送らない
                self.end_headers()
                self.wfile.write(body)
            elif self.path.startswith("/shot/"):
                name = self.path[len("/shot/"):]
                f = shots.SHOT_DIR / name
                if "/" in name or ".." in name or not name.endswith(".png") or not f.exists():
                    self.send_error(404)
                    return
                body = f.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_error(404)

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
