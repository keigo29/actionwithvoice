"""ウェイクワード（openWakeWord・手元で動くので無料）。呼ばれたら、黙るまで聞き取って渡す"""

from __future__ import annotations

import array
import threading
from typing import Callable

from .audio import RATE, SilenceDetector, rms

FRAME = 1280  # 80ms。openWakeWord がこの長さで判定する


def is_builtin(model: str) -> bool:
    """openWakeWord に最初から入っている呼び名（hey_jarvis など）か。ファイルの道筋なら False"""
    return bool(model) and "/" not in model and not model.endswith((".onnx", ".tflite"))


class WakeListener:
    def __init__(self, cfg: dict, on_wake: Callable[[], None], on_audio: Callable[[bytes], None],
                 is_busy: Callable[[], bool]):
        w = cfg["wake"]
        if not w["model"]:
            raise RuntimeError("wake.model にウェイクワードのモデルを指定してください（README 参照）")
        from openwakeword.model import Model  # 使うときだけ読み込む

        if is_builtin(w["model"]):
            from openwakeword.utils import download_models
            download_models([w["model"]])  # 最初の1回だけ取ってくる（数MB）
        # tflite は Mac の Python で入らないことが多いので、.tflite のファイルを指したときだけ使う
        framework = "tflite" if w["model"].endswith(".tflite") else "onnx"
        self.model = Model(wakeword_models=[w["model"]], inference_framework=framework)
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
