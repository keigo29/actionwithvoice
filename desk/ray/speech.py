"""読み上げ（macOS の say）と、読み上げ向けの文の整形"""

from __future__ import annotations

import queue
import re
import subprocess
import threading

from . import guard

_URL = re.compile(r"https?://\S+")
_CODE = re.compile(r"```.*?```", re.S)
_MD = re.compile(r"[*_`#>|]+")
_LIST = re.compile(r"^\s*(?:[-・•]|\d+[.)])\s+", re.M)


def clean_for_speech(text: str) -> str:
    """Markdown の記号や URL を、読み上げで変にならない形にする"""
    text = _CODE.sub("（コードは画面で見てください）", text)
    text = _URL.sub("リンク", text)
    text = _LIST.sub("", text)
    text = _MD.sub("", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[\[([^\]]+)\]\]", r"\1", text)
    text = re.sub(r"\s*\n+\s*", "。", text)
    text = re.sub(r"。{2,}", "。", text)
    text = re.sub(r"[ \t]{2,}", " ", text).lstrip("。 ").rstrip()
    return guard.to_japanese(text)


class Speaker:
    """順番に読み上げる。stop() で今の読み上げと待ちを全部止める"""

    def __init__(self, cfg: dict, popen=subprocess.Popen):
        self.voice = cfg["tts"]["voice"]
        self.rate = str(cfg["tts"]["rate"])
        self.popen = popen
        self.q: queue.Queue[str | None] = queue.Queue()
        self.proc = None
        self.lock = threading.Lock()
        self.idle = threading.Event()
        self.idle.set()
        threading.Thread(target=self._run, daemon=True).start()

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if text:
            self.idle.clear()
            self.q.put(text)

    def _run(self) -> None:
        while True:
            text = self.q.get()
            if text is None:
                return
            with self.lock:
                self.proc = self.popen(["say", "-v", self.voice, "-r", self.rate, text])
            self.proc.wait()
            with self.lock:
                self.proc = None
            if self.q.empty():
                self.idle.set()

    def stop(self) -> None:
        while not self.q.empty():
            try:
                self.q.get_nowait()
            except queue.Empty:
                break
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
        self.idle.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.idle.wait(timeout)
