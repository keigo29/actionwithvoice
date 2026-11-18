"""効果音：映画の AI の画面のような電子音を、その場で合成して鳴らす

音はファイルを持たず、起動のたびに Python だけで作る（numpy などは使わない）。
鳴らすのは macOS の afplay。読み上げ（say）と重なっても大丈夫。

  boot   起動したとき。低いうなりが立ち上がり、データの粒が走って、高い和音で開く
  open   画面（カード）を出すとき。上がるチャープ＋短い風切り＋カチッ
  close  画面を閉じるとき。open の逆向き
  listen 聞き取りを始めたとき。短い2つのピッ
  ack    聞き取れて、考え始めたとき。小さなピロッ
"""

from __future__ import annotations

import math
import random
import shutil
import struct
import subprocess
import tempfile
import threading
import wave
from pathlib import Path

RATE = 44100
NAMES = ("boot", "open", "close", "listen", "ack")


# ---------------------------------------------------------------- 部品

def _env(n: int, attack: float, release: float, curve: float = 3.0) -> list[float]:
    """立ち上がりと消え方（秒）。消え方は指数のカーブ"""
    a = max(1, int(attack * RATE))
    out = []
    for i in range(n):
        if i < a:
            g = i / a
        else:
            t = (i - a) / max(1, n - a)
            g = math.exp(-curve * t) * (1 - t) if release else 1.0
        out.append(g)
    return out


def _tone(dur: float, f0: float, f1: float | None = None, attack=0.005, curve=4.0,
          shape: str = "sine", amp: float = 1.0) -> list[float]:
    """f0 から f1 へ音の高さが動く音"""
    n = int(dur * RATE)
    f1 = f0 if f1 is None else f1
    env = _env(n, attack, 1, curve)
    ph, out = 0.0, []
    for i in range(n):
        f = f0 * (f1 / f0) ** (i / max(1, n - 1))  # 耳に自然な、比で動くスイープ
        ph += 2 * math.pi * f / RATE
        if shape == "sine":
            v = math.sin(ph)
        elif shape == "tri":
            v = 2 / math.pi * math.asin(math.sin(ph))
        else:  # 少しだけ倍音のある、ガラスのような音
            v = math.sin(ph) + 0.3 * math.sin(2 * ph) + 0.12 * math.sin(3.01 * ph)
        out.append(v * env[i] * amp)
    return out


def _noise(dur: float, lo: float, hi: float, attack=0.01, curve=4.0, amp=1.0, seed=0) -> list[float]:
    """帯域を絞ったノイズ（風切り・空気の音）。lo/hi は通す周波数のおおよその下と上"""
    rnd = random.Random(seed)
    n = int(dur * RATE)
    env = _env(n, attack, 1, curve)
    a_hi = math.exp(-2 * math.pi * hi / RATE)
    a_lo = math.exp(-2 * math.pi * lo / RATE)
    lp = lp2 = 0.0
    out = []
    for i in range(n):
        x = rnd.uniform(-1, 1)
        lp = (1 - a_hi) * x + a_hi * lp     # 上を削る
        lp2 = (1 - a_lo) * lp + a_lo * lp2  # 下を削る（差を取る）
        out.append((lp - lp2) * 3.0 * env[i] * amp)
    return out


def _click(amp=1.0, freq=3200.0) -> list[float]:
    return _tone(0.012, freq, freq * 0.6, attack=0.0005, curve=9, amp=amp)


def _mix(dst: list[float], src: list[float], at: float, gain: float = 1.0) -> None:
    s = int(at * RATE)
    need = s + len(src)
    if need > len(dst):
        dst.extend([0.0] * (need - len(dst)))
    for i, v in enumerate(src):
        dst[s + i] += v * gain


def _stereo(mono: list[float], delay=0.11, feedback=0.35, wet=0.25, tail=0.4) -> tuple[list[float], list[float]]:
    """左右で少しずらした響き（ピンポンのディレイ）で、画面の奥行きを出す"""
    mono = mono + [0.0] * int(tail * RATE)
    d = int(delay * RATE)
    left, right = list(mono), list(mono)
    bl, br = [0.0] * len(mono), [0.0] * len(mono)
    for i in range(len(mono)):
        if i >= d:
            bl[i] = mono[i - d] + br[i - d] * feedback
            br[i] = bl[i - d] * feedback
        left[i] += bl[i] * wet
        right[i] += br[i] * wet * 1.4
    return left, right


def _write(path: Path, left: list[float], right: list[float], peak_db: float = -3.0) -> Path:
    peak = max(1e-9, max(max(abs(v) for v in left), max(abs(v) for v in right)))
    g = 10 ** (peak_db / 20) / peak
    fade = int(0.01 * RATE)  # 最後のプツッを防ぐ
    n = len(left)
    frames = bytearray()
    for i in range(n):
        f = min(1.0, (n - i) / fade)
        frames += struct.pack("<hh", int(left[i] * g * f * 32767), int(right[i] * g * f * 32767))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(bytes(frames))
    return path


# ---------------------------------------------------------------- 音

def boot() -> list[float]:
    out: list[float] = []
    _mix(out, _tone(1.6, 55, 110, attack=0.6, curve=2.5, shape="tri"), 0.0, 0.55)   # 低いうなりが立ち上がる
    _mix(out, _tone(1.6, 82.5, 165, attack=0.6, curve=2.5), 0.0, 0.25)
    _mix(out, _noise(1.1, 300, 2500, attack=0.9, curve=6), 0.0, 0.10)                # 吸い込むような風
    rnd = random.Random(7)
    t = 0.35
    while t < 1.05:                                                                  # データの粒が走る
        f = rnd.choice([1760, 2093, 2349, 2637, 3136, 3520])
        _mix(out, _tone(0.035, f, f * rnd.choice([1.0, 1.5, 0.75]), attack=0.001, curve=7, shape="glass"),
             t, 0.3 + 0.3 * (t - 0.35))
        t += rnd.uniform(0.025, 0.06)
    _mix(out, _click(1.0, 4200), 1.08, 0.6)                                          # カチッと開く
    for f, g in ((880, 0.32), (1318.5, 0.26), (1760, 0.2), (2637, 0.12)):            # 高い和音で開く
        _mix(out, _tone(1.1, f, f * 1.003, attack=0.008, curve=3.5, shape="glass"), 1.1, g)
    _mix(out, _noise(0.3, 5000, 12000, attack=0.002, curve=9), 1.08, 0.05)
    return out


def open_() -> list[float]:
    out: list[float] = []
    _mix(out, _noise(0.2, 1200, 5000, attack=0.1, curve=7), 0.0, 0.12)               # 短い風切り
    _mix(out, _tone(0.16, 600, 2400, attack=0.004, curve=5, shape="glass"), 0.02, 0.45)  # 上がるチャープ
    _mix(out, _click(1.0, 3600), 0.15, 0.7)                                           # カチッと止まる
    _mix(out, _tone(0.25, 2400, 2400, attack=0.002, curve=7, shape="glass"), 0.155, 0.22)
    return out


def close() -> list[float]:
    out: list[float] = []
    _mix(out, _click(0.8, 3000), 0.0, 0.6)
    _mix(out, _tone(0.18, 2000, 500, attack=0.002, curve=4, shape="glass"), 0.01, 0.4)  # 下がるチャープ
    _mix(out, _noise(0.18, 800, 4000, attack=0.01, curve=6), 0.02, 0.10)
    return out


def listen() -> list[float]:
    out: list[float] = []
    _mix(out, _tone(0.06, 1568, attack=0.002, curve=5, shape="glass"), 0.0, 0.5)
    _mix(out, _tone(0.09, 2349, attack=0.002, curve=5, shape="glass"), 0.07, 0.5)
    return out


def ack() -> list[float]:
    out: list[float] = []
    _mix(out, _tone(0.05, 2093, 2637, attack=0.002, curve=6, shape="glass"), 0.0, 0.35)
    _mix(out, _tone(0.08, 3136, attack=0.002, curve=6, shape="glass"), 0.045, 0.3)
    return out


SOUNDS = {"boot": boot, "open": open_, "close": close, "listen": listen, "ack": ack}


def render(folder: Path) -> dict[str, Path]:
    """全部の音を folder に WAV で書く"""
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, make in SOUNDS.items():
        wet = 0.32 if name == "boot" else 0.2
        left, right = _stereo(make(), wet=wet, tail=0.5 if name == "boot" else 0.25)
        paths[name] = _write(folder / f"{name}.wav", left, right)
    return paths


class Sfx:
    """効果音を鳴らす。afplay が無い（Mac 以外）ときや、切ってあるときは何もしない"""

    def __init__(self, cfg: dict, popen=subprocess.Popen, which=shutil.which):
        c = cfg.get("sfx", {})
        self.enabled = bool(c.get("enabled", True)) and bool(which("afplay"))
        self.volume = float(c.get("volume", 0.5))
        self.popen = popen
        self.paths: dict[str, Path] = {}
        self.ready = threading.Event()
        if self.enabled:
            folder = Path(tempfile.gettempdir()) / "ray-sfx"
            threading.Thread(target=self._prepare, args=(folder,), daemon=True).start()

    def _prepare(self, folder: Path) -> None:
        try:
            self.paths = render(folder)
        except Exception as e:  # 効果音が作れなくても、会話は止めない
            print(f"効果音を作れませんでした：{e}")
        finally:
            self.ready.set()

    def play(self, name: str, wait: float = 0.0) -> None:
        if not self.enabled:
            return
        if wait:
            self.ready.wait(wait)
        p = self.paths.get(name)
        if p:
            self.popen(["afplay", "-v", str(self.volume), str(p)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
