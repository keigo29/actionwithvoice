"""サーバーで動く Ray（Mac を通さずに、iPhone から使う）

    python -m ray --server

- 頭（振り分け・トークンを使わない頼みごと・Gemini・Claude Code）は Mac の Ray と同じ。マイク・スピーカー・キーは使わない
- iPhone の Safari で Ray の画面を開く。聞き取りと読み上げは iPhone がする（Whisper・say・VOICEVOX は使わない）
- 読み上げる文は、その文の場面（画面）と一緒に画面へ送る。iPhone は、その文を話し始めるときに、その場面を出す
- 入口は合言葉（[server] token）でログインした人だけ。外に開くときは Tailscale（tailscale serve）の中だけにする
"""

from __future__ import annotations

import signal
import sys
import threading

from . import speech
from .app import Assistant
from .ui import StateHub, serve


class RemoteSpeaker:
    """声は鳴らさず、文と、その文の場面を画面（iPhone）へ送る。鳴らすのも、鳴り終わりを待つのも iPhone"""

    def __init__(self, hub: StateHub):
        self.hub = hub
        self.lock = threading.Lock()
        self.n = 0
        self.cut = 0

    def say(self, text: str, on_start=None) -> None:
        text = speech.clean_for_speech(text)
        with self.lock:
            if on_start:
                on_start()  # 場面を決める（サーバーの状態を、この文の場面にする）
            if text:
                self.n += 1
                self.hub.set(utter={"id": self.n, "text": text, "scene": self.hub.state.get("scene")})

    def stop(self) -> None:
        with self.lock:
            self.cut += 1
            self.hub.set(cut=self.cut)  # iPhone は、話している途中と順番待ちを止める

    def wait(self, timeout=None) -> bool:
        return True


class RemoteRay:
    """iPhone からの頼みを受けて、Ray の頭に渡す（1つずつ。前のが動いていたら止めてから）"""

    def __init__(self, assistant: Assistant):
        self.a = assistant
        self.worker: threading.Thread | None = None
        self.lock = threading.Lock()

    def ask(self, text: str) -> None:
        with self.lock:
            if self.worker and self.worker.is_alive():
                self.a.interrupt()
                self.worker.join(timeout=5)
            self.worker = threading.Thread(target=self._run, args=(text,), daemon=True)
            self.worker.start()

    def _run(self, text: str) -> None:
        self.a.busy.set()
        try:
            self.a.handle_text(text)
        except Exception as e:  # noqa: BLE001 1つの失敗で、受け口ごと止めない
            print(f"答えられませんでした：{e}")
            self.a.hub.set(state="idle", text="")
        finally:
            self.a.busy.clear()

    def stop(self) -> None:
        self.a.interrupt()
        self.a.hub.set(state="idle", text="")


class TypedRay(RemoteRay):
    """机の Ray の入力欄からの頼み。声の頼みと同時に動かないよう、同じ作業の糸を使う"""

    def ask(self, text: str) -> None:
        with self.lock:
            if self.a.busy.is_set() or (self.a.worker and self.a.worker.is_alive()):
                self.a.interrupt()
                if self.a.worker:
                    self.a.worker.join(timeout=5)
            else:
                self.a.speaker.stop()
            self.a.worker = threading.Thread(target=self._run, args=(text,), daemon=True)
            self.a.worker.start()


def run(cfg: dict) -> None:
    from .__main__ import make_brain

    s = cfg["server"]
    if len(s.get("token") or "") < 16:
        sys.exit("config.toml の [server] token に、16文字以上の合言葉を書いてください"
                 "（python3 -c 'import secrets; print(secrets.token_urlsafe(24))' で作れる）")
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    hub = StateHub()
    hub.set(theme=cfg["ui"].get("theme", "blueprint"))
    brain = make_brain(cfg)
    assistant = Assistant(cfg, brain, RemoteSpeaker(hub), hub)
    remote = RemoteRay(assistant)
    if cfg["ui"].get("usage", True):
        from .usage import UsageWatcher
        UsageWatcher(hub, gemini=getattr(brain, "gemini", None), search=getattr(brain, "search", None)).start()
    serve(hub, s["port"], host=s["listen"],
          on_notify=assistant.notify if cfg["notify"]["enabled"] else None, notify_cfg=cfg["notify"],
          remote={"token": s["token"], "ask": remote.ask, "stop": remote.stop})
    print(f"Ray（サーバー）を起動しました：http://{s['listen']}:{s['port']}/  （iPhone では tailscale serve の https のアドレスで開く）")
    threading.Event().wait()
