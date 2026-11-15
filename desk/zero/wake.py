"""ウェイクワード（openWakeWord・手元で動くので無料）。呼ばれたら、黙るまで聞き取って渡す"""

from __future__ import annotations

import array
import threading
from typing import Callable

from .audio import RATE, SilenceDetector, rms

FRAME = 1280  # 80ms。openWakeWord がこの長さで判定する


class WakeListener:
    def __init__(self, cfg: dict, on_wake: Callable[[], None], on_audio: Callable[[bytes], None],
                 is_busy: Callable[[], bool]):
        w = cfg["wake"]
        if not w["model"]:
            raise RuntimeError("wake.model にウェイクワードのモデルを指定してください（README 参照）")
        from openwakeword.model import Model  # 使うときだけ読み込む

        self.model = Model(wakeword_models=[w["model"]], inference_framework="onnx" if w["model"].endswith(".onnx") else "tflite")
        self.cfg = w
        self.on_wake = on_wake
        self.on_audio = on_audio
        self.is_busy = is_busy
        self.stop_flag = threading.Event()

    def run(self) -> None:
        import sounddevice as sd

        capturing = None
        buf: list[bytes] = []
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
            while not self.stop_flag.is_set():
                data, _ = stream.read(FRAME)
                data = bytes(data)
                samples = array.array("h", data)
                if capturing is not None:
                    buf.append(data)
                    if capturing.feed(rms(samples)):
                        audio = b"".join(buf)
                        capturing, buf = None, []
                        self.model.reset()
                        self.on_audio(audio)
                    continue
                if self.is_busy():
                    continue
                import numpy as np

                scores = self.model.predict(np.frombuffer(data, dtype=np.int16))
                if max(scores.values(), default=0) >= self.cfg["threshold"]:
                    self.on_wake()
                    capturing = SilenceDetector(FRAME / RATE, silence_sec=self.cfg["silence_sec"], max_sec=self.cfg["max_sec"])
                    buf = []

    def stop(self) -> None:
        self.stop_flag.set()
