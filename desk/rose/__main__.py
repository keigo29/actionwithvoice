"""起動：python -m rose（desk フォルダで）

  python -m rose            # 常駐（キーを押している間に話す／ウェイクワード）
  python -m rose --text 今日のデイリー読んで   # マイクなしで1回だけ試す
  python -m rose --check    # 準備ができているか確かめる
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

from . import config as config_mod
from .app import Assistant
from .brain import Brain
from .speech import Speaker
from .ui import StateHub, serve


def check(cfg: dict) -> bool:
    ok = True

    def line(good: bool, label: str, hint: str = "") -> None:
        nonlocal ok
        ok &= good
        print(f"{'✅' if good else '❌'} {label}" + (f" … {hint}" if hint and not good else ""))

    line(bool(shutil.which(cfg["claude"]["bin"])), "Claude Code（claude）", "npm i -g @anthropic-ai/claude-code して claude でログイン")
    if os.environ.get("ANTHROPIC_API_KEY"):
        print("⚠️  ANTHROPIC_API_KEY が設定されています。ローズは子プロセスに渡さないので Pro の枠で動きます")
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
        line(bool(cfg["wake"]["model"]) and Path(cfg["wake"]["model"]).exists(), "ウェイクワードのモデル", "wake.model を指定")
    return ok


def key_matcher(name: str):
    from pynput import keyboard

    target = getattr(keyboard.Key, name, None) or keyboard.KeyCode.from_char(name)
    return lambda key: key == target


def run(cfg: dict, use_ui: bool) -> None:
    from .audio import Recorder
    from pynput import keyboard

    hub = StateHub()
    speaker = Speaker(cfg)
    assistant = Assistant(cfg, Brain(cfg), speaker, hub)
    if use_ui and cfg["ui"]["enabled"]:
        serve(hub, cfg["ui"]["port"])
        url = f"http://localhost:{cfg['ui']['port']}/"
        print(f"状態の画面：{url}")
        if cfg["ui"]["open_browser"]:
            webbrowser.open(url)

    if cfg["wake"]["enabled"]:
        from .wake import WakeListener

        wl = WakeListener(cfg, on_wake=assistant.interrupt, on_audio=assistant.submit_audio,
                          is_busy=assistant.busy.is_set)
        threading.Thread(target=wl.run, daemon=True).start()
        print("ウェイクワードで呼べます")

    recorder = Recorder()
    is_hotkey = key_matcher(cfg["hotkey"])
    pressed = False

    def on_press(key):
        nonlocal pressed
        if is_hotkey(key) and not pressed:
            pressed = True
            assistant.interrupt()
            recorder.start()

    def on_release(key):
        nonlocal pressed
        if is_hotkey(key) and pressed:
            pressed = False
            assistant.submit_audio(recorder.stop())

    print(f"「{cfg['hotkey']}」キーを押している間に話してください（終わるときは Ctrl+C）")
    speaker.say("ローズです。どうぞ")
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        try:
            listener.join()
        except KeyboardInterrupt:
            print("おしまい")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rose", description="デスク常駐型の音声アシスタント")
    p.add_argument("--config", help="設定ファイル（既定：desk/config.toml）")
    p.add_argument("--text", nargs="+", help="マイクを使わずに、この文で1回だけ聞く")
    p.add_argument("--check", action="store_true", help="準備ができているか確かめる")
    p.add_argument("--no-ui", action="store_true", help="状態の画面を出さない")
    p.add_argument("--quiet", action="store_true", help="--text のとき読み上げない")
    a = p.parse_args(argv)
    cfg = config_mod.load(a.config)

    if a.check:
        return 0 if check(cfg) else 1
    if a.text:
        class Silent:
            def say(self, t): pass
            def stop(self): pass
            def wait(self, timeout=None): return True

        speaker = Silent() if a.quiet or not shutil.which("say") else Speaker(cfg)
        assistant = Assistant(cfg, Brain(cfg), speaker, StateHub())
        answer = assistant.handle_text(" ".join(a.text))
        print(answer or "")
        speaker.wait(timeout=120)
        return 0
    run(cfg, use_ui=not a.no_ui)
    return 0


if __name__ == "__main__":
    sys.exit(main())
