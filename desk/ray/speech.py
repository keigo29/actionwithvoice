"""読み上げ（macOS の say か VOICEVOX）と、読み上げ向けの文の整形"""

from __future__ import annotations

import array
import json
import math
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import wave
from pathlib import Path
from typing import Callable

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


LIPSYNC_STEP_MS = 40  # 声の大きさを測る間隔（画面の円の内側の光を、これに合わせて光らせる）


def envelope(path: str, step_ms: int = LIPSYNC_STEP_MS) -> list[float]:
    """WAVE（16ビット）の声の大きさを、step_ms ごとに 0〜1 で返す（いちばん大きいところが1）"""
    with wave.open(path, "rb") as w:
        rate, ch, width = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if width != 2 or not raw:
        return []
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    n = max(1, rate * ch * step_ms // 1000)
    rms = []
    for i in range(0, len(samples), n):
        chunk = samples[i:i + n]
        rms.append(math.sqrt(sum(x * x for x in chunk) / len(chunk)) if chunk else 0.0)
    top = max(rms, default=0) or 1.0
    return [round(min(1.0, (r / top) ** .7), 2) for r in rms]  # 小さめの音も見えるよう、少し持ち上げる


# 「傘はいりません」を VOICEVOX は「傘 入りません（ハイリマセン）」と読む。漢字・カタカナのあとの「はいら／はいり」は
# 「要る」にして、「は」を「わ」と読ませる（「いる」は「居る」もあるので変えない）
_HA_IRU = re.compile(r"(?<=[一-龯々ァ-ヶー])はい(?=ら|り)")


def fix_reading(text: str) -> str:
    """声を作る前に、読み違えやすい書き方を直す"""
    return _HA_IRU.sub("は要", text)


def voicevox_wav(text: str, path: str, c: dict, urlopen=urllib.request.urlopen) -> bool:
    """VOICEVOX（手元のアプリ）で声を作って WAVE に書き出す。だめなら False"""
    base, spk = c["url"].rstrip("/"), str(c["speaker"])
    q = urllib.parse.urlencode({"text": fix_reading(text), "speaker": spk})
    with urlopen(urllib.request.Request(f"{base}/audio_query?{q}", data=b"", method="POST"), timeout=c["timeout"]) as r:
        query = json.loads(r.read())
    query["speedScale"] = c.get("speed", 1.0)
    req = urllib.request.Request(f"{base}/synthesis?speaker={spk}", data=json.dumps(query).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req, timeout=c["timeout"]) as r:
        data = r.read()
    if not data.startswith(b"RIFF"):
        return False
    Path(path).write_bytes(data)
    return True


def add_echo(path: str, delay_ms: int = 180, decay: float = 0.35, repeats: int = 3) -> bool:
    """声のファイルにエコー（少し遅れて小さくなる声の重なり）をかける。16ビットのモノラルだけ。かけたら True"""
    with wave.open(path, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            return False
        params = w.getparams()
        src = array.array("h", w.readframes(w.getnframes()))
    if sys.byteorder == "big":
        src.byteswap()
    step = int(params.framerate * delay_ms / 1000)
    if step <= 0 or repeats <= 0 or not src:
        return False
    n = len(src)
    out = [float(x) for x in src] + [0.0] * (step * repeats)  # 響きが消えるまで、少し長くする
    gain = 1.0
    for r in range(1, repeats + 1):
        gain *= decay
        off = step * r
        for i in range(n):
            out[off + i] += src[i] * gain
    peak = max(1.0, max(abs(x) for x in out))
    scale = min(1.0, 32767 / peak)  # 重ねて割れるときだけ、全体を少し小さくする
    res = array.array("h", (int(x * scale) for x in out))
    if sys.byteorder == "big":
        res.byteswap()
    with wave.open(path, "wb") as w:
        w.setparams(params)
        w.writeframes(res.tobytes())
    return True


def voicevox_up(c: dict, urlopen=urllib.request.urlopen) -> bool:
    try:
        with urlopen(c["url"].rstrip("/") + "/version", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


class Speaker:
    """順番に読み上げる。stop() で今の読み上げと待ちを全部止める

    on_voice があり、afplay が使えるとき（Mac）は、声をいったんファイルに書き出して、
    声の大きさ（envelope）を測ってから鳴らす。鳴らし始めに on_voice(大きさの並び, 間隔ミリ秒) を呼ぶ
    （画面の円の内側の光を、発音に合わせて光らせるため）。次の文の書き出しは、今の文を鳴らしている間に進める。
    書き出せなかったときや、on_voice が無いときは、これまでどおり say で直接読み上げる。
    """

    def __init__(self, cfg: dict, popen=subprocess.Popen, on_voice: Callable[[list[float], int], None] | None = None,
                 which=shutil.which, urlopen=urllib.request.urlopen):
        self.voice = cfg["tts"]["voice"]
        self.rate = str(cfg["tts"]["rate"])
        self.popen = popen
        self.on_voice = on_voice
        self.urlopen = urlopen
        # VOICEVOX：声をファイルで受け取って afplay で鳴らす（だめな文は say で）
        self.vv = cfg["tts"]["voicevox"] if cfg["tts"].get("engine") == "voicevox" and which("afplay") else None
        self.vv_warned = False
        self.echo = cfg["tts"].get("echo") or {}  # [tts.echo] enabled = true でエコーをかける
        self.lipsync = bool(self.vv) or (
            bool(on_voice) and cfg["tts"].get("lipsync", True) and bool(which("afplay")) and bool(which("say")))
        self.q: queue.Queue = queue.Queue()
        self.ready: queue.Queue = queue.Queue(maxsize=2)  # 書き出し済みで、鳴らす番を待つもの
        self.proc = None
        self.synth = None
        self.lock = threading.Lock()
        self.gen = 0          # stop() のたびに増やす。古い番号のものは鳴らさない
        self.pending = 0      # まだ鳴らし終わっていない文の数
        self.idle = threading.Event()
        self.idle.set()
        self.tmp = Path(tempfile.mkdtemp(prefix="ray-say-")) if self.lipsync else None
        if self.vv:
            threading.Thread(target=self._start_voicevox, daemon=True).start()
        if self.lipsync:
            threading.Thread(target=self._synth_loop, daemon=True).start()
            threading.Thread(target=self._play_loop, daemon=True).start()
        else:
            threading.Thread(target=self._run, daemon=True).start()

    def say(self, text: str) -> None:
        text = clean_for_speech(text)
        if text:
            with self.lock:
                self.pending += 1
                self.idle.clear()
                gen = self.gen
            self.q.put((gen, text))

    def _done_one(self, gen: int) -> None:
        """1文を鳴らし終えた（stop() の前の番号のものは数えない）"""
        with self.lock:
            if gen != self.gen:
                return
            self.pending = max(0, self.pending - 1)
            if self.pending == 0:
                self.idle.set()

    def _speak_direct(self, text: str) -> None:
        with self.lock:
            self.proc = self.popen(["say", "-v", self.voice, "-r", self.rate, text])
        self.proc.wait()
        with self.lock:
            self.proc = None

    def _run(self) -> None:
        """書き出さずに say で直接読み上げる（これまでの形）"""
        while True:
            gen, text = self.q.get()
            if gen == self.gen:
                self._speak_direct(text)
            self._done_one(gen)

    def _start_voicevox(self) -> None:
        """VOICEVOX が起動していなければ、裏で起動しておく（準備できるまでの文は say で読む）"""
        if voicevox_up(self.vv, self.urlopen) or sys.platform != "darwin":
            return
        try:
            self.popen(["open", "-g", "-a", self.vv.get("app", "VOICEVOX")])
        except Exception as e:  # 起動できなくても、say で読み上げる
            print(f"VOICEVOX を起動できませんでした：{e}")

    def _write(self, text: str, path: str) -> bool:
        """1文の声をファイルに書き出す。VOICEVOX がだめなら say で書き出す"""
        if self.vv:
            try:
                if voicevox_wav(text, path, self.vv, self.urlopen):
                    return True
            except Exception as e:
                if not self.vv_warned:
                    print(f"VOICEVOX で声を作れませんでした（Mac の声で読みます）：{e}")
                    self.vv_warned = True
        with self.lock:
            self.synth = self.popen(["say", "-v", self.voice, "-r", self.rate, "--file-format=WAVE",
                                     "--data-format=LEI16@22050", "-o", path, text])
        ok = self.synth.wait() == 0
        with self.lock:
            self.synth = None
        return ok

    def _synth_loop(self) -> None:
        n = 0
        while True:
            gen, text = self.q.get()
            if gen != self.gen:
                continue
            n += 1
            path = str(self.tmp / f"{n % 8}.wav")
            env = None
            try:
                ok = self._write(text, path)
                if ok and self.echo.get("enabled") and os.path.exists(path):
                    try:
                        add_echo(path, int(self.echo.get("delay_ms", 180)), float(self.echo.get("decay", 0.35)),
                                 int(self.echo.get("repeats", 3)))
                    except Exception as e:  # エコーをかけられなくても、そのまま読む
                        print(f"エコーをかけられませんでした：{e}")
                env = envelope(path) if ok and os.path.exists(path) else None
            except Exception as e:  # 書き出せなくても、読み上げは止めない
                print(f"声の書き出しに失敗しました（そのまま読み上げます）：{e}")
            self.ready.put((gen, text, path if env else None, env))

    def _play_loop(self) -> None:
        while True:
            gen, text, path, env = self.ready.get()
            if gen == self.gen:
                if path:
                    try:
                        if self.on_voice:
                            self.on_voice(env, LIPSYNC_STEP_MS)
                    except Exception:
                        pass
                    with self.lock:
                        self.proc = self.popen(["afplay", path])
                    self.proc.wait()
                    with self.lock:
                        self.proc = None
                else:
                    self._speak_direct(text)
            self._done_one(gen)

    def stop(self) -> None:
        with self.lock:
            self.gen += 1      # これより前の文は、もう鳴らさない・数えない
            self.pending = 0
        for q in (self.q, self.ready):
            while not q.empty():
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
        with self.lock:
            for p in (self.proc, self.synth):
                if p and p.poll() is None:
                    p.terminate()
        self.idle.set()

    def wait(self, timeout: float | None = None) -> bool:
        return self.idle.wait(timeout)
