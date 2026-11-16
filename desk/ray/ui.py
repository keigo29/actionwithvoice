"""状態表示：光る円の HTML 1枚と、状態を流す SSE（http://localhost:8765）"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import shots

PAGE = Path(__file__).resolve().parent / "ui" / "index.html"


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


def serve(hub: StateHub, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/":
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

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server
