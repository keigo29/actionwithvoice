"""マイクの録音（押している間）と、黙ったら止める判定"""

from __future__ import annotations

import math
import threading
import wave
from pathlib import Path

RATE = 16000  # Whisper も openWakeWord も 16kHz・モノラル
MIN_SEC = 0.4  # これより短い録音は押し間違いとして捨てる


def rms(samples) -> float:
    """int16 の並び → 音の大きさ（0〜32768）"""
    n = len(samples)
    if n == 0:
        return 0.0
    return math.sqrt(sum(float(x) * float(x) for x in samples) / n)


class SilenceDetector:
    """話し始めたあと、silence_sec 黙ったら終わり。max_sec で打ち切り。話さないまま wait_sec で諦める"""

    def __init__(self, frame_sec: float, threshold: float = 500, silence_sec: float = 1.2,
                 max_sec: float = 15, wait_sec: float = 5):
        self.frame_sec = frame_sec
        self.threshold = threshold
        self.silence_sec = silence_sec
        self.max_sec = max_sec
        self.wait_sec = wait_sec
        self.elapsed = 0.0
        self.quiet = 0.0
        self.spoke = False

    def feed(self, level: float) -> bool:
        """1コマ分の音の大きさを入れる。終わりなら True"""
        self.elapsed += self.frame_sec
        if level >= self.threshold:
            self.spoke = True
            self.quiet = 0.0
        else:
            self.quiet += self.frame_sec
        if self.elapsed >= self.max_sec:
            return True
        if not self.spoke:
            return self.elapsed >= self.wait_sec
        return self.quiet >= self.silence_sec


def save_wav(path: str | Path, frames: bytes) -> Path:
    path = Path(path)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(frames)
    return path


def duration(frames: bytes) -> float:
    return len(frames) / 2 / RATE


class Recorder:
    """start() から stop() までの音をためる（プッシュトゥトーク用）"""

    def __init__(self):
        import sounddevice as sd  # Mac でだけ使う

        self.sd = sd
        self.chunks: list[bytes] = []
        self.stream = None
        self.lock = threading.Lock()

    def _cb(self, indata, frames, time_info, status):
        with self.lock:
            self.chunks.append(bytes(indata))

    def start(self) -> None:
        with self.lock:
            self.chunks = []
        self.stream = self.sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", callback=self._cb)
        self.stream.start()

    def stop(self) -> bytes:
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None
        with self.lock:
            return b"".join(self.chunks)
