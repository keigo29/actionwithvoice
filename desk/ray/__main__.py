"""起動：python -m ray（desk フォルダで）

  python -m ray            # 常駐（キーを押している間に話す／ウェイクワード）
  python -m ray --text 今日のデイリー読んで   # マイクなしで1回だけ試す
  python -m ray --check    # 準備ができているか確かめる
"""

from __future__ import annotations

import argparse
import atexit
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

from . import config as config_mod
from .app import Assistant
from .brain import Brain
from .local import HybridBrain
from .sfx import Sfx
from .speech import Speaker
from .ui import StateHub, serve


def make_brain(cfg: dict):
    """ローカル LLM か Gemini を使う設定なら、ローカル → Gemini 無料枠 → Claude の振り分けをつける"""
    claude = Brain(cfg)
    if not (cfg["local"]["enabled"] or cfg["gemini"]["enabled"]):
        return claude
    from .local import GeminiBrain
    brain = HybridBrain(cfg, claude, gemini=GeminiBrain(cfg, store=GEMINI_COUNT_FILE))
    brain.warm()  # 裏で読み込んでおく（読み終わるまでは Claude が答える）
    atexit.register(brain.close)  # Ray を止めたら、メモリから外す
    return brain


def check(cfg: dict) -> bool:
    ok = True

    def line(good: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'✅' if good else '❌'} {label}" + (f" … {hint}" if hint and not good else ""))

    line(bool(shutil.which(cfg["claude"]["bin"])), "Claude Code（claude）", "npm i -g @anthropic-ai/claude-code して claude でログイン")
    if cfg["codex"]["enabled"]:
        line(bool(shutil.which(cfg["codex"]["bin"])), "Codex（codex）", "brew install --cask codex して codex login")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY が設定されています。Rayは子プロセスに渡さないので Pro の枠で動きます")
    if cfg["local"]["enabled"]:
        try:
            with urllib.request.urlopen(cfg["local"]["url"].rstrip("/") + "/api/tags", timeout=2) as r:
                names = [m["name"] for m in json.loads(r.read()).get("models", [])]
            m = cfg["local"]["model"]
            line(m in names or f"{m}:latest" in names, f"ローカル LLM（Ollama・{m}）", f"ollama pull {m}")
        except OSError:
            line(False, "ローカル LLM（Ollama）", "brew install ollama して ollama serve（使わないなら [local] enabled = false）")
    if cfg["gemini"]["enabled"]:
        from .local import GeminiBrain

        if GeminiBrain(cfg).key:
            print(f"✅ Gemini 無料枠（{cfg['gemini']['model']}）")
        else:
            print("⚠️  Gemini の鍵がありません（Gemini の段は飛ばして Claude が答えます）"
                  " … AI Studio で課金なしのプロジェクトの鍵を作り、環境変数 GEMINI_API_KEY に入れる")
    if cfg["search"]["enabled"]:
        from .search import SearchBrain

        if SearchBrain(cfg, None).key:
            print(f"✅ 検索 API（{cfg['search']['provider']}）")
        else:
            print("⚠️  検索 API の鍵がありません（調べものは Gemini の検索 → Claude で答えます）"
                  " … Tavily で鍵を作り、環境変数 TAVILY_API_KEY に入れる")
    line(bool(shutil.which("say")), "読み上げ（say）", "macOS で動かしてください")
    cmd = Path(cfg["cmd_dir"])
    line((cmd / ".git").exists(), f"Cmd のクローン（{cmd}）", "config.toml の cmd_dir を直す")
    try:
        from .stt import pick_engine

        line(True, f"音声入力（{pick_engine(cfg)}）")
    except RuntimeError as e:
        line(False, "音声入力", str(e))
    for mod, label in (("sounddevice", "マイク（sounddevice）"), ("pynput", "キー入力（pynput）")):
        line(importlib.util.find_spec(mod) is not None, label, "pip install -r requirements.txt")
    if cfg["wake"]["enabled"]:
        line(importlib.util.find_spec("openwakeword") is not None, "ウェイクワード（openwakeword）", "pip install openwakeword")
        from .wake import is_builtin
        m = cfg["wake"]["model"]
        line(bool(m) and (is_builtin(m) or Path(m).exists()), f"ウェイクワードのモデル（{m or 'なし'}）",
             "wake.model に hey_jarvis などの内蔵の呼び名か、学習した .onnx を指定")
    if cfg["notify"]["enabled"]:
        line(len(cfg["notify"]["token"]) >= 16, f"n8n からの知らせ（POST http://127.0.0.1:{cfg['ui']['port']}/notify）",
             "[notify] token に16文字以上の合言葉を入れる（python3 -c 'import secrets; print(secrets.token_urlsafe(24))'）")
        listen = cfg["notify"].get("listen", "")
        if listen:
            import socket

            try:
                with socket.socket(socket.AF_INET6 if ":" in listen else socket.AF_INET) as s:
                    s.bind((listen, 0))
                can = True
            except OSError:
                can = False
            line(can, f"外からの知らせ（POST http://{listen}:{cfg['ui']['port']}/notify）",
                 "Tailscale を起動して、tailscale ip -4 の IP を [notify] listen に入れる")
    return ok


def key_matcher(name: str):
    from pynput import keyboard

    target = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)
    return lambda key: key == target


class PushToTalk:
    """キーを押している間の録音。キーの知らせを受ける側では何もせず、別の糸で順に片づける

    キーの知らせ（pynput）の中でマイクを開いたり音を鳴らしたりすると、メモリが足りないとき
    （ローカル LLM を読み込んだ直後など）に時間がかかり、macOS がキーの監視を止めてしまう。
    そうなると「離した」が届かず、「聞いています」のまま動かなくなる。
    念のため、離したのが届かなくても max_hold 秒で区切って聞き取りに回す。
    """

    def __init__(self, assistant, recorder, max_hold: float = 30.0, stuck_file=None, stuck_sec: float = 10.0):
        import queue

        self.stuck_file = stuck_file  # 1つの手順が stuck_sec 秒を超えたら、全部の糸の居場所をここに書く
        self.stuck_sec = stuck_sec
        self.assistant = assistant
        self.recorder = recorder
        self.max_hold = max_hold
        self.q: queue.Queue = queue.Queue()
        self.pressed = False      # キーの側（押した・離したの重なりを捨てる）
        self.recording = False    # 片づける側
        threading.Thread(target=self._loop, daemon=True).start()

    def press(self) -> None:
        if not self.pressed:
            self.pressed = True
            self.q.put("press")

    def release(self) -> None:
        if self.pressed:
            self.pressed = False
            self.q.put("release")

    def _loop(self) -> None:
        import queue

        while True:
            try:
                ev = self.q.get(timeout=self.max_hold if self.recording else None)
            except queue.Empty:
                print(f"キーを離したのが届かないので、{self.max_hold:.0f}秒で区切りました")
                self.pressed = False
                ev = "release"
            if self.stuck_file is not None:
                import faulthandler

                faulthandler.dump_traceback_later(self.stuck_sec, file=self.stuck_file)
            try:
                print(f"（キー：{'押した' if ev == 'press' else '離した'}）")
                if ev == "press" and not self.recording:
                    self.assistant.interrupt()
                    self.recorder.start()
                    self.recording = True
                elif ev == "release" and self.recording:
                    self.recording = False
                    self.assistant.submit_audio(self.recorder.stop())
            except Exception as e:  # マイクのエラーで、キーの受け付けまで止めない
                print(f"録音のエラー：{e}")
                self.recording = False
                self.assistant.hub.set(state="idle", text="")
            finally:
                if self.stuck_file is not None:
                    faulthandler.cancel_dump_traceback_later()


class _Tee:
    """ターミナルに出す内容を、時刻つきでファイルにも残す"""

    def __init__(self, stream, file):
        self.stream, self.file, self.fresh = stream, file, True

    def write(self, s: str) -> int:
        self.stream.write(s)
        out = []
        for part in s.splitlines(keepends=True):
            if self.fresh:
                out.append(time.strftime("%H:%M:%S "))
            out.append(part)
            self.fresh = part.endswith("\n")
        self.file.write("".join(out))
        self.file.flush()
        return len(s)

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


LOG_FILE = Path(__file__).resolve().parent.parent / "ray.log"
GEMINI_COUNT_FILE = Path(__file__).resolve().parent.parent / "gemini-count.json"


def start_log(path: Path = LOG_FILE):
    """ray.log に残す。止まったときは kill -USR1 <pid> で、全部の糸の居場所も書き出せる"""
    import faulthandler

    f = open(path, "a", encoding="utf-8", buffering=1)
    f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 起動（pid {os.getpid()}） =====\n")
    sys.stdout = _Tee(sys.stdout, f)
    sys.stderr = _Tee(sys.stderr, f)
    faulthandler.register(signal.SIGUSR1, file=f, all_threads=True)
    return f


def log_states(hub) -> None:
    """画面の状態が変わるたびに記録する"""
    plain = hub.set

    def set(**kw):
        before = hub.state.get("state")
        plain(**kw)
        if "state" in kw and kw["state"] != before:
            print(f"［状態］{kw['state']} {kw.get('text', '')}".rstrip())
    hub.set = set


def run(cfg: dict, use_ui: bool) -> None:
    from .audio import Recorder
    from pynput import keyboard

    # kill で止められたときも、終わりの片づけ（ローカル LLM を外す）をする
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    log = start_log()
    hub = StateHub()
    hub.set(theme=cfg["ui"].get("theme", "neon"))  # 画面の見た目（URL の ?theme= があれば、そちらが優先）
    log_states(hub)
    # 鳴らし始めに、声の大きさの並びを画面へ送る（円の内側の光を発音に合わせて光らせる）
    voice_no = iter(range(1, 10**9))
    speaker = Speaker(cfg, on_voice=lambda env, step: hub.set(voice={"id": next(voice_no), "step": step, "env": env}))
    sfx = Sfx(cfg)
    brain = make_brain(cfg)
    assistant = Assistant(cfg, brain, speaker, hub, sfx=sfx)
    if cfg.get("follow_sec", 10) > 0:
        from .audio import listen_once
        assistant.follow = lambda stop: listen_once(wait_sec=cfg["follow_sec"], should_stop=stop)
    from . import stt
    threading.Thread(target=stt.warm, args=(cfg,), daemon=True).start()  # 聞き取りのモデルを先に読み込む
    notify = cfg["notify"]["enabled"]
    show = use_ui and cfg["ui"]["enabled"]
    if show:
        from .usage import UsageWatcher
        if cfg["ui"].get("usage", True):
            UsageWatcher(hub, gemini=getattr(brain, "gemini", None), search=getattr(brain, "search", None)).start()
    if show or notify:
        serve(hub, cfg["ui"]["port"], on_notify=assistant.notify if notify else None, notify_cfg=cfg["notify"])
    if notify:
        print(f"n8n からの知らせを受けます：POST http://127.0.0.1:{cfg['ui']['port']}/notify")
        listen = cfg["notify"].get("listen", "")
        if listen:
            try:  # サーバーの n8n から Tailscale を通って届く知らせ
                serve(hub, cfg["ui"]["port"], on_notify=assistant.notify, notify_cfg=cfg["notify"],
                      host=listen, notify_only=True)
                print(f"外からの知らせも受けます：POST http://{listen}:{cfg['ui']['port']}/notify")
            except OSError as e:
                print(f"⚠️  {listen} で知らせを受けられません（Tailscale はつながっていますか？）：{e}")
    if show:
        url = f"http://localhost:{cfg['ui']['port']}/"
        print(f"状態の画面：{url}")
        if cfg["ui"]["open_browser"]:
            open_page(url, cfg["ui"].get("browser", ""))

    if cfg["wake"]["enabled"]:
        from .wake import WakeListener

        wl = WakeListener(cfg, on_wake=assistant.interrupt, on_audio=assistant.submit_audio,
                          is_busy=assistant.busy.is_set)
        threading.Thread(target=wl.run, daemon=True).start()
        print("ウェイクワードで呼べます")

    ptt = PushToTalk(assistant, Recorder(), stuck_file=log)
    is_hotkey = key_matcher(cfg["hotkey"])

    def on_press(key):
        if is_hotkey(key):
            ptt.press()

    def on_release(key):
        if is_hotkey(key):
            ptt.release()

    print(f"「{cfg['hotkey']}」キーを押している間に話してください（終わるときは Ctrl+C）")
    sfx.play("boot", wait=3)  # 起動の音（作り終わるのを少しだけ待つ）。名乗りはしない
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("おしまい")


def open_page(url: str, app: str = "", run=subprocess.run) -> None:
    """状態の画面を開く。app（"Safari" など）があれば Mac でそのアプリで開く。だめなら、ふだんのブラウザ"""
    if app and sys.platform == "darwin":
        try:
            if run(["open", "-a", app, url], capture_output=True, timeout=10).returncode == 0:
                return
        except (OSError, subprocess.SubprocessError):
            pass
        print(f"{app} で開けなかったので、ふだんのブラウザで開きます")
    webbrowser.open(url)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="ray", description="デスク常駐型の音声アシスタント")
    p.add_argument("--config", help="設定ファイル（既定：desk/config.toml）")
    p.add_argument("--text", nargs="+", help="マイクを使わずに、この文で1回だけ聞く")
    p.add_argument("--check", action="store_true", help="準備ができているか確かめる")
    p.add_argument("--no-ui", action="store_true", help="状態の画面を出さない")
    p.add_argument("--quiet", action="store_true", help="--text のとき読み上げない")
    p.add_argument("--prompt-report", action="store_true", help="送ったプロンプトの効率を、記録からまとめる")
    p.add_argument("--days", type=int, default=7, help="--prompt-report で見る日数（既定7）")
    p.add_argument("--sync-workstation", metavar="DIR", help="Workstation のチェックアウトから、図を描く部分を写し直す")
    a = p.parse_args(argv)
    if a.sync_workstation:
        from . import wsdiagram
        for name in wsdiagram.sync(a.sync_workstation):
            print(f"写しました：ray/ui/vendor/{name}")
        return 0
    cfg = config_mod.load(a.config)

    if a.check:
        return 0 if check(cfg) else 1
    if a.prompt_report:
        from . import promptcheck
        print(promptcheck.run(cfg, a.days))
        return 0
    if a.text:
        class Silent:
            def say(self, t, on_start=None): on_start and on_start()
            def stop(self): pass
            def wait(self, timeout=None): return True

        speaker = Silent() if a.quiet or not shutil.which("say") else Speaker(cfg)
        brain = make_brain(cfg)
        if isinstance(brain, HybridBrain):
            brain.warm(block=True)  # 1回だけなので、読み込みを待ってからローカルで試す
        assistant = Assistant(cfg, brain, speaker, StateHub())
        answer = assistant.handle_text(" ".join(a.text))
        print(answer or "")
        speaker.wait(timeout=120)
        return 0
    run(cfg, use_ui=not a.no_ui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
