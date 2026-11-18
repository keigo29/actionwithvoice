"""効果音：近未来のホログラムの画面のような電子音を、その場で合成して鳴らす

音はファイルを持たず、Python だけで作る（numpy などは使わない）。一度作った音は
一時フォルダに取っておき、次の起動からはそれを使う。鳴らすのは macOS の afplay。

SF らしさを作っている要素（映画の HUD の録画の分析と、この種の音の定番から）と、ここでの作り方：

  1 金属的でこの世のものでない響き  … FM 合成で、整数でない倍音（ベルやガラスのような響き）
  2 光がちらつく感じ               … 速いトレモロ（20 Hz 前後の音量のゆれ）
  3 きらめき・厚み                 … 少しずらした2つの音のうなり（デチューン）
  4 デジタルの粗さ                 … ビットクラッシュ（音の段を粗くし、ざらつかせる）
  5 データが処理されている感じ     … ランダムな高さの短い粒が速く並ぶ（サンプル＆ホールド）
  6 画面が「ズッ」と開く感じ       … 共鳴の強いフィルターを、ノイズに対して速く動かす
  7 カチッという立ち上がり         … 録画では 2.7 秒に 14 回。短いクリックを要所に置く
  8 ずっと鳴っている高い電子音     … 録画では 6〜10 kHz に持続音。起動の最後に細く残す
  9 グリッチ                       … ごく短い切れ端を3回くり返す
 10 空間に投影された広がり         … 左右に跳ねるディレイと、残響（リバーブ）

  boot   起動したとき
  open   画面（カード）を出すとき
  close  画面を閉じるとき
  listen 聞き取りを始めたとき
  ack    聞き取れて、考え始めたとき
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
VERSION = 2  # 音の作り方を変えたら上げる（取っておいた音を作り直す）
PITCH = 0.5  # 音の高さの倍率（0.5 で1オクターブ下）。config の sfx.pitch で変えられる
_pitch = PITCH
NAMES = ("boot", "open", "close", "listen", "ack")
TAU = 2 * math.pi


# ---------------------------------------------------------------- 部品

def _n(dur: float) -> int:
    return max(1, int(dur * RATE))


def _env(n: int, attack: float, curve: float = 4.0) -> list[float]:
    """立ち上がり（秒）と、指数で消えていくカーブ"""
    a = max(1, int(attack * RATE))
    out = []
    for i in range(n):
        if i < a:
            out.append(i / a)
        else:
            t = (i - a) / max(1, n - a)
            out.append(math.exp(-curve * t) * (1 - t))
    return out


def _hz(f: float, fixed: bool) -> float:
    return f if fixed else f * _pitch


def _tone(dur, f0, f1=None, attack=0.005, curve=4.0, amp=1.0, detune=0.0, fixed=False,
          trem=0.0, trem_depth=0.0) -> list[float]:
    """f0→f1 に動く音。detune（セント）で2つの音のうなり、trem（Hz）でちらつき"""
    n = _n(dur)
    f0 = _hz(f0, fixed)
    f1 = f0 if f1 is None else _hz(f1, fixed)
    env = _env(n, attack, curve)
    ratios = [1.0] if not detune else [2 ** (-detune / 1200), 2 ** (detune / 1200)]
    phs = [0.0] * len(ratios)
    out = []
    for i in range(n):
        f = f0 * (f1 / f0) ** (i / max(1, n - 1))
        v = 0.0
        for k, r in enumerate(ratios):
            phs[k] += TAU * f * r / RATE
            v += math.sin(phs[k])
        v /= len(ratios)
        if trem:
            v *= 1 - trem_depth * (0.5 + 0.5 * math.sin(TAU * trem * i / RATE))
        out.append(v * env[i] * amp)
    return out


def _fm(dur, carrier, ratio=1.41, index=3.0, attack=0.002, curve=5.0, amp=1.0, index_decay=4.0,
        glide=1.0, fixed=False, trem=0.0, trem_depth=0.0) -> list[float]:
    """FM 合成。ratio が整数でないと、金属やガラスのような、この世のものでない響きになる。
    index は明るさ（倍音の多さ）で、時間とともに暗くなる"""
    n = _n(dur)
    c0 = _hz(carrier, fixed)
    env = _env(n, attack, curve)
    pc = pm = 0.0
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        c = c0 * glide ** t
        pm += TAU * c * ratio / RATE
        pc += TAU * c / RATE
        idx = index * math.exp(-index_decay * t)
        v = math.sin(pc + idx * math.sin(pm))
        if trem:
            v *= 1 - trem_depth * (0.5 + 0.5 * math.sin(TAU * trem * i / RATE))
        out.append(v * env[i] * amp)
    return out


def _zip(dur, f0, f1, q=8.0, attack=0.01, curve=4.0, amp=1.0, seed=0, fixed=False) -> list[float]:
    """共鳴の強いフィルター（状態変数フィルター）をノイズに通し、f0→f1 に速く動かす。「ズッ」「シュイン」"""
    rnd = random.Random(seed)
    n = _n(dur)
    f0, f1 = _hz(f0, fixed), _hz(f1, fixed)
    env = _env(n, attack, curve)
    low = band = 0.0
    damp = 1 / q
    out = []
    for i in range(n):
        f = f0 * (f1 / f0) ** (i / max(1, n - 1))
        k = 2 * math.sin(math.pi * min(f, RATE / 6) / RATE)
        x = rnd.uniform(-1, 1)
        low += k * band
        high = x - low - damp * band
        band += k * high
        out.append(band * env[i] * amp * 0.5)
    return out


def _crush(src: list[float], bits: int = 6, hold: int = 4) -> list[float]:
    """ビットクラッシュ：音の段を粗くし（bits）、同じ値を hold 個ずつ続ける。デジタルのざらつき"""
    levels = 2 ** (bits - 1)
    out, last = [], 0.0
    for i, v in enumerate(src):
        if i % hold == 0:
            last = round(v * levels) / levels
        out.append(last)
    return out


def _data(dur, lo, hi, step=(0.018, 0.045), seed=1, amp=1.0, crush=True) -> list[float]:
    """データ処理の粒：ランダムな高さの短い FM の音を、速く並べる（サンプル＆ホールド）"""
    rnd = random.Random(seed)
    out: list[float] = []
    t = 0.0
    while t < dur:
        f = rnd.uniform(lo, hi)
        blip = _fm(rnd.uniform(0.012, 0.03), f, ratio=rnd.choice([1.41, 2.76, 3.5]), index=rnd.uniform(1, 4),
                   curve=7, amp=amp * rnd.uniform(0.5, 1.0))
        _mix(out, blip, t)
        t += rnd.uniform(*step)
    return _crush(out, 7, 3) if crush else out


def _click(amp=1.0, freq=3000.0) -> list[float]:
    """カチッ。とても短い、高い音の立ち上がり"""
    return _tone(0.008, freq, freq * 0.5, attack=0.0003, curve=10, amp=amp, fixed=True)


def _stutter(src: list[float], at: float, length: float = 0.025, times: int = 3) -> list[float]:
    """グリッチ：at 秒からの短い切れ端を times 回くり返して差し込む"""
    s, ln = int(at * RATE), _n(length)
    piece = src[s:s + ln]
    return src[:s] + piece * times + src[s + ln:]


def _mix(dst: list[float], src: list[float], at: float = 0.0, gain: float = 1.0) -> None:
    s = int(at * RATE)
    need = s + len(src)
    if need > len(dst):
        dst.extend([0.0] * (need - len(dst)))
    for i, v in enumerate(src):
        dst[s + i] += v * gain


def _comb(x, d, fb, damp):
    buf, out, idx, lp = [0.0] * d, [], 0, 0.0
    for v in x:
        y = buf[idx]
        lp = y * (1 - damp) + lp * damp
        buf[idx] = v + lp * fb
        idx = (idx + 1) % d
        out.append(y)
    return out


def _allpass(x, d, g=0.5):
    buf, out, idx = [0.0] * d, [], 0
    for v in x:
        b = buf[idx]
        y = -v + b
        buf[idx] = v + b * g
        idx = (idx + 1) % d
        out.append(y)
    return out


def _reverb(x: list[float], size: float = 0.8, spread: int = 0) -> list[float]:
    """小さなリバーブ（4つのくし形フィルター＋2つのオールパス）。空間に投影された広がり"""
    combs = [1116, 1188, 1277, 1356]
    acc = [0.0] * len(x)
    for d in combs:
        for i, v in enumerate(_comb(x, d + spread, 0.70 + 0.12 * size, 0.3)):
            acc[i] += v * 0.25
    for d in (556, 441):
        acc = _allpass(acc, d + spread)
    return acc


def _space(mono: list[float], tail: float, delay=0.11, feedback=0.35, wet=0.25, verb=0.25,
           size=0.8) -> tuple[list[float], list[float]]:
    """左右に跳ねるディレイと、左右で少し違うリバーブ"""
    mono = mono + [0.0] * _n(tail)
    d = _n(delay)
    left, right = list(mono), list(mono)
    bl, br = [0.0] * len(mono), [0.0] * len(mono)
    for i in range(len(mono)):
        if i >= d:
            bl[i] = mono[i - d] + br[i - d] * feedback
            br[i] = bl[i - d] * feedback
        left[i] += bl[i] * wet
        right[i] += br[i] * wet * 1.3
    if verb:
        rl, rr = _reverb(mono, size), _reverb(mono, size, spread=23)
        for i in range(len(mono)):
            left[i] += rl[i] * verb
            right[i] += rr[i] * verb
    return left, right


def _write(path: Path, left: list[float], right: list[float], peak_db: float = -3.0) -> Path:
    peak = max(1e-9, max(max(abs(v) for v in left), max(abs(v) for v in right)))
    g = 10 ** (peak_db / 20) / peak
    fade = _n(0.02)  # 最後のプツッを防ぐ
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
    # 低いうなりが、うなりを伴って立ち上がる（Mac のスピーカーでも聞こえる高さに固定）
    _mix(out, _tone(1.7, 80, 160, attack=0.7, curve=2.5, detune=12, fixed=True), 0.0, 0.45)
    _mix(out, _tone(1.7, 120, 240, attack=0.7, curve=2.5, detune=8, fixed=True), 0.0, 0.18)
    # 共鳴フィルターが下から上へ走る（エネルギーが溜まっていく）
    _mix(out, _zip(1.15, 300, 5000, q=10, attack=0.9, curve=7, seed=3), 0.0, 0.45)
    # データの粒（後半ほど密に、大きく）
    _mix(out, _data(0.7, 1400, 3600, step=(0.02, 0.05), seed=7), 0.4, 0.22)
    _mix(out, _data(0.35, 1800, 4400, step=(0.012, 0.025), seed=8), 0.78, 0.3)
    # カチッの連打で「つながる」
    for k, t in enumerate((1.02, 1.07, 1.10, 1.12)):
        _mix(out, _click(1.0, 3600 + 300 * k), t, 0.5)
    # 開く：FM のガラスの和音。ちらつき（トレモロ）とうなりで、光の投影のように
    for f, g in ((440, 0.36), (659.3, 0.26), (880, 0.22), (1318.5, 0.14)):
        _mix(out, _fm(1.4, f, ratio=1.41, index=2.2, attack=0.004, curve=3.2, index_decay=3,
                      trem=19, trem_depth=0.35), 1.12, g)
    _mix(out, _tone(1.4, 880, 880, attack=0.01, curve=3, detune=9), 1.12, 0.12)
    # 最後に細く残る高い電子音（録画の 6〜10 kHz の持続音にあたる）
    _mix(out, _tone(1.2, 7000, 7000, attack=0.2, curve=3, fixed=True, trem=31, trem_depth=0.5), 1.25, 0.025)
    return _stutter(out, 1.12, 0.022, 3)  # 開く瞬間にグリッチ


def open_() -> list[float]:
    out: list[float] = []
    _mix(out, _zip(0.2, 500, 6000, q=9, attack=0.12, curve=6, seed=11), 0.0, 0.55)   # ズッと開く
    _mix(out, _fm(0.14, 700, ratio=2.76, index=3, glide=2.2, curve=5), 0.03, 0.35)   # 上がる金属のチャープ
    _mix(out, _click(1.0, 3400), 0.15, 0.6)
    _mix(out, _click(0.7, 4200), 0.175, 0.45)
    _mix(out, _fm(0.35, 1760, ratio=1.41, index=1.6, curve=6, trem=22, trem_depth=0.4), 0.155, 0.22)
    _mix(out, _data(0.12, 2000, 4000, step=(0.012, 0.02), seed=12), 0.17, 0.12)       # 中身が流れ込む
    return out


def close() -> list[float]:
    out: list[float] = []
    _mix(out, _click(0.8, 3200), 0.0, 0.55)
    _mix(out, _fm(0.18, 1600, ratio=2.76, index=3, glide=0.3, curve=4), 0.01, 0.35)  # 下がる金属のチャープ
    _mix(out, _zip(0.2, 5000, 400, q=9, attack=0.01, curve=5, seed=13), 0.02, 0.45)  # シュンと閉じる
    return _crush(out, 8, 2)


def listen() -> list[float]:
    out: list[float] = []
    _mix(out, _fm(0.07, 1568, ratio=1.41, index=1.5, curve=5), 0.0, 0.5)
    _mix(out, _fm(0.1, 2349, ratio=1.41, index=1.5, curve=5, trem=24, trem_depth=0.3), 0.075, 0.5)
    return out


def ack() -> list[float]:
    out: list[float] = []
    for k, f in enumerate((2093, 2637, 3136)):  # 速い3つの粒（処理を始めた合図）
        _mix(out, _fm(0.04, f, ratio=2.76, index=1.2, curve=6), 0.03 * k, 0.32)
    return _crush(out, 7, 3)


SOUNDS = {"boot": boot, "open": open_, "close": close, "listen": listen, "ack": ack}
SPACE = {  # 名前 → (残りの響きの長さ, ディレイの量, リバーブの量)
    "boot": (0.9, 0.3, 0.35), "open": (0.35, 0.22, 0.22), "close": (0.3, 0.18, 0.18),
    "listen": (0.25, 0.15, 0.12), "ack": (0.2, 0.15, 0.1),
}


def render(folder: Path, pitch: float = PITCH) -> dict[str, Path]:
    """全部の音を folder に WAV で書く。pitch は音の高さの倍率"""
    global _pitch
    _pitch = pitch
    folder.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, make in SOUNDS.items():
        tail, wet, verb = SPACE[name]
        left, right = _space(make(), tail, wet=wet, verb=verb)
        paths[name] = _write(folder / f"{name}.wav", left, right)
    return paths


class Sfx:
    """効果音を鳴らす。afplay が無い（Mac 以外）ときや、切ってあるときは何もしない"""

    def __init__(self, cfg: dict, popen=subprocess.Popen, which=shutil.which, folder: Path | None = None):
        c = cfg.get("sfx", {})
        self.enabled = bool(c.get("enabled", True)) and bool(which("afplay"))
        self.volume = float(c.get("volume", 0.5))
        self.pitch = float(c.get("pitch", PITCH))
        self.popen = popen
        self.paths: dict[str, Path] = {}
        self.ready = threading.Event()
        if self.enabled:
            folder = folder or Path(tempfile.gettempdir()) / f"ray-sfx-v{VERSION}-p{self.pitch:g}"
            threading.Thread(target=self._prepare, args=(folder,), daemon=True).start()

    def _prepare(self, folder: Path) -> None:
        try:
            have = {n: folder / f"{n}.wav" for n in NAMES}
            if all(p.exists() for p in have.values()):  # 前に作った音があれば使う
                self.paths = have
            else:
                self.paths = render(folder, self.pitch)
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
