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

グリッチ（起動以外の短い音）：同じ切れ端のくり返し・一瞬の途切れ・一瞬のデジタルの粗さ・逆再生・
テープが止まるような速度の落ち方。切れ目は数ミリ秒でなめらかにつなぎ、「カチッ」にしない。

音の手ざわりは「カチッ」ではなく「ファン」：立ち上がりを丸くし、明るさがふくらんでから閉じ、
息の音を重ね、高いところを削り、残響を多めにする（_bloom・_breath）。7 と 9 は今は使っていない。

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
VERSION = 9  # 音の作り方を変えたら上げる（取っておいた音を作り直す）
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


# ---------------------------------------------------------------- やわらかい音（「ファン」）の部品

def _lowpass(src: list[float], cutoff: float) -> list[float]:
    """高いところを削って、輪郭をやわらげる（1次のローパス）"""
    a = math.exp(-TAU * cutoff / RATE)
    y, out = 0.0, []
    for v in src:
        y = (1 - a) * v + a * y
        out.append(y)
    return out


def _swell(n: int, attack: float, curve: float = 3.0) -> list[float]:
    """ふくらんで、なめらかに消える形（立ち上がりも丸い）"""
    a = max(1, _n(attack))
    out = []
    for i in range(n):
        if i < a:
            out.append(0.5 - 0.5 * math.cos(math.pi * i / a))  # 丸い立ち上がり
        else:
            t = (i - a) / max(1, n - a)
            out.append(math.exp(-curve * t) * (1 - t) ** 0.5)
    return out


def _bloom(dur, f0, f1=None, attack=0.06, curve=3.0, ratio=1.0, index=1.6, open_at=0.3, detune=7.0,
           air=0.35, air_q=3.0, fixed=False, trem=0.0, trem_depth=0.0) -> list[float]:
    """「ファン」：明るさ（FM の倍音）がふくらんでから閉じていく音に、同じ高さの息の音を重ねる。
    少しずらした2つの音でうなりを出し、全体を丸く削る"""
    n = _n(dur)
    a0 = _hz(f0, fixed)
    a1 = a0 if f1 is None else _hz(f1, fixed)
    env = _swell(n, attack, curve)
    rs = (2 ** (-detune / 1200), 2 ** (detune / 1200))
    pc, pm = [0.0, 0.0], [0.0, 0.0]
    rnd = random.Random(int(f0))
    low = band = 0.0
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        f = a0 * (a1 / a0) ** t
        # 明るさは open_at でいちばん開き、そのあと閉じる（「ファ」→「ン」）
        bright = math.sin(math.pi * min(1.0, t / (2 * open_at))) if t < 2 * open_at else 0.15
        v = 0.0
        for k in (0, 1):
            pm[k] += TAU * f * rs[k] * ratio / RATE
            pc[k] += TAU * f * rs[k] / RATE
            v += math.sin(pc[k] + index * bright * math.sin(pm[k]))
        v *= 0.5
        # 息：同じ高さのまわりだけ通したノイズ
        k2 = 2 * math.sin(math.pi * min(f * 2, RATE / 6) / RATE)
        low += k2 * band
        band += k2 * (rnd.uniform(-1, 1) - low - band / air_q)
        v += band * air * (0.4 + 0.6 * bright)
        if trem:
            v *= 1 - trem_depth * (0.5 + 0.5 * math.sin(TAU * trem * i / RATE))
        out.append(v * env[i])
    return _lowpass(out, _hz(f0, fixed) * 6 + 1500)


def _breath(dur, lo, hi, attack=0.15, curve=3.0, seed=0) -> list[float]:
    """空気が流れるような「フワッ」。丸い立ち上がりのノイズを、低い方から高い方へ通す"""
    rnd = random.Random(seed)
    n = _n(dur)
    env = _swell(n, attack, curve)
    low = band = 0.0
    out = []
    for i in range(n):
        f = _hz(lo, False) * ((hi / lo) ** (i / max(1, n - 1)))
        k = 2 * math.sin(math.pi * min(f, RATE / 6) / RATE)
        low += k * band
        band += k * (rnd.uniform(-1, 1) - low - band / 2.0)
        out.append(band * env[i] * 0.6)
    return out


# ---------------------------------------------------------------- 音

def boot() -> list[float]:
    out: list[float] = []
    # 低いうなりが、うなりを伴ってゆっくりふくらむ（Mac のスピーカーでも聞こえる高さに固定）
    _mix(out, _tone(2.0, 80, 160, attack=0.9, curve=2.2, detune=12, fixed=True), 0.0, 0.45)
    _mix(out, _tone(2.0, 120, 240, attack=0.9, curve=2.2, detune=8, fixed=True), 0.0, 0.16)
    # 空気が吸い込まれていく
    _mix(out, _breath(1.3, 200, 3000, attack=1.0, curve=6, seed=3), 0.0, 0.5)
    # データの粒は、遠くでやわらかく
    _mix(out, _lowpass(_data(0.7, 1400, 3200, step=(0.03, 0.06), seed=7, crush=False), 2500), 0.45, 0.12)
    # 開く：「ファーン」と和音がふくらむ。ゆっくりしたちらつきで、光の投影のように
    for f, g in ((440, 0.4), (659.3, 0.3), (880, 0.22), (1318.5, 0.1)):
        _mix(out, _bloom(2.0, f, f * 1.01, attack=0.12, curve=2.6, ratio=1.41, index=1.4, open_at=0.18,
                         trem=7, trem_depth=0.18), 1.05, g)
    _mix(out, _breath(1.2, 800, 5000, attack=0.1, curve=4, seed=4), 1.03, 0.25)
    return out


# 起動以外は、0.5 秒で終わるさりげない音。楽器のように「音符から音符へ」跳ねると近未来感が消えるので、
# 決まった高さを持たない音で作る：
#   ・響きの強いフィルターが、息（ノイズ）と金属的な FM の混ざった音を一気に通り過ぎる（「フィッ」）
#   ・リングモジュレーションで、少しだけ金属のざらつき
#   ・左から右へ（閉じるときは右から左へ）音が動く
#   ・起動と同じ、遠くのデータの粒とゆらぎ
# 高すぎないよう、フィルターの行き先は 2 kHz 前後までにし、全体の高いところも削る

def _fwip(dur, f0, f1, q=6.0, attack=0.02, curve=4.0, noise=0.25, ring=90.0, ring_mix=0.3, shape=0.5,
          seed=0) -> list[float]:
    """フィッ：共鳴フィルター（f0→f1、決まった高さを持たない）を、ノイズと FM の混ざった音に通す。
    shape が小さいほど、最初に速く動いてあとはゆっくり（すっと抜ける感じ）"""
    rnd = random.Random(seed)
    n = _n(dur)
    env = _swell(n, attack, curve)
    low = band = 0.0
    pc = pm = 0.0
    out = []
    for i in range(n):
        t = i / max(1, n - 1)
        f = f0 * (f1 / f0) ** (t ** shape)
        # 音のもと：ノイズと、フィルターの少し下で鳴る、整数でない倍音の FM
        pm += TAU * f * 0.5 * 2.76 / RATE
        pc += TAU * f * 0.5 / RATE
        src = noise * rnd.uniform(-1, 1) + (1 - noise) * math.sin(pc + 2.0 * math.sin(pm))
        k = 2 * math.sin(math.pi * min(f, RATE / 6) / RATE)
        low += k * band
        band += k * (src - low - band / q)
        v = band * (1 - ring_mix + ring_mix * math.sin(TAU * ring * i / RATE))  # リングモジュレーション
        out.append(v * env[i])
    return _lowpass(out, 4000)


def _grains(dur, n_grains, lo, hi, seed=0) -> list[float]:
    """起動と同じ、遠くのデータの粒（やわらかく）"""
    out: list[float] = []
    rnd = random.Random(seed)
    for k in range(n_grains):
        f = rnd.uniform(lo, hi)
        g = _bloom(0.05, f, f * rnd.uniform(0.9, 1.15), attack=0.02, curve=6, ratio=2.76, index=1.2, air=0.08,
                   fixed=True, open_at=0.2)
        _mix(out, g, dur * k / max(1, n_grains), rnd.uniform(0.6, 1.0))
    return _lowpass(out, 3000)


# ---------------------------------------------------------------- グリッチ（壊れかけたデジタルの感じ）
# どれも切れ目を数ミリ秒でなめらかにつなぎ、「カチッ」にならないようにする

def _fade_edges(seg: list[float], ms: float = 3.0) -> list[float]:
    f = max(1, _n(ms / 1000))
    out = list(seg)
    for i in range(min(f, len(out))):
        g = i / f
        out[i] *= g
        out[-1 - i] *= g
    return out


def _g_repeat(src, at, length, times):
    """同じ切れ端をくり返す（どもる）"""
    s, ln = _n(at), _n(length)
    piece = _fade_edges(src[s:s + ln])
    return src[:s] + piece * times + src[s + ln:]


def _g_drop(src, at, length):
    """一瞬だけ音が途切れる"""
    s, ln, f = _n(at), _n(length), _n(0.003)
    out = list(src)
    for i in range(s, min(len(out), s + ln)):
        d = min(i - s, s + ln - i)
        out[i] *= 1 - min(1.0, d / f)
    return out


def _g_crush(src, at, length, bits=4, hold=12):
    """一瞬だけデジタルが粗くなる（ビットクラッシュとサンプルレートの低下）"""
    s, ln = _n(at), _n(length)
    seg = _lowpass(_crush(src[s:s + ln], bits, hold), 5000)  # 粗くしても、耳に刺さらないよう少し削る
    f = max(1, _n(0.004))
    out = list(src)
    for i, v in enumerate(seg):
        if s + i >= len(out):
            break
        w = min(1.0, i / f, (len(seg) - i) / f)
        out[s + i] = out[s + i] * (1 - w) + v * w
    return out


def _g_reverse(src, at, length):
    """切れ端を逆さに再生する"""
    s, ln = _n(at), _n(length)
    piece = _fade_edges(src[s:s + ln][::-1])
    return src[:s] + piece + src[s + ln:]


def _g_speed(src, at, length, start=1.0, end=0.3):
    """速度がずれる（テープが止まるように、だんだん遅く・低く）"""
    s, ln = _n(at), _n(length)
    seg = src[s:s + ln]
    out_seg, pos = [], 0.0
    while pos < len(seg) - 1:
        t = len(out_seg) / max(1, ln)
        rate = start + (end - start) * min(1.0, t)
        i = int(pos)
        frac = pos - i
        out_seg.append(seg[i] * (1 - frac) + seg[i + 1] * frac)
        pos += rate
    return src[:s] + _fade_edges(out_seg, 5) + src[s + ln:]


# 起動以外の4つは、「聞き始め」をお手本にそろえる：
#   芯のある短いフィッ（息は3割）＋ グリッチはどもり1回だけ。向き（上・下）と長さ・幅で区別する

def _cue(dur, f0, f1, stutter_at, stutter_len=0.014, times=3, shape=0.5, noise=0.3, seed=0) -> list[float]:
    """お手本（聞き始め）と同じ作りの音：フィッ＋最後の小さなどもり"""
    out: list[float] = []
    _mix(out, _fwip(dur, f0, f1, q=5, attack=0.035, curve=4.5, noise=noise, ring_mix=0.2, shape=shape, seed=seed), 0.0, 1.0)
    return _g_repeat(out, stutter_at, stutter_len, times)


def listen() -> list[float]:
    """聞き始め（お手本）：息を吸うような短いフィッの最後が、小さくどもる"""
    return _cue(0.15, 400, 1300, 0.07, seed=21)


def open_() -> list[float]:
    """画面を開く：お手本より少し長く、広く上へ。音は左から右へ"""
    return _cue(0.18, 330, 1600, 0.085, seed=11)


def close() -> list[float]:
    """画面を閉じる：お手本を下向きに。音は右から左へ"""
    return _cue(0.17, 1500, 330, 0.08, shape=0.6, seed=13)


def ack() -> list[float]:
    """聞き取れた：いちばん短く狭いフィッ。どもりは2回"""
    return _cue(0.1, 650, 1150, 0.045, stutter_len=0.012, times=2, noise=0.18, seed=22)


SOUNDS = {"boot": boot, "open": open_, "close": close, "listen": listen, "ack": ack}
PAN = {"open": (-0.6, 0.6, 0.3), "close": (0.6, -0.6, 0.3)}  # 名前 → (始めの位置, 終わりの位置, 動く秒数)。-1 が左


def _pan(left, right, p0, p1, dur):
    """音を左右に動かす（-1 が左、1 が右）"""
    n = _n(dur)
    out_l, out_r = [], []
    for i, (l, r) in enumerate(zip(left, right)):
        p = p0 + (p1 - p0) * min(1.0, i / n)
        a = (p + 1) * math.pi / 4
        out_l.append(l * math.cos(a) * 1.41)
        out_r.append(r * math.sin(a) * 1.41)
    return out_l, out_r


PEAK_DB = {"open": -9.0, "close": -10.0, "listen": -10.0, "ack": -12.0}  # 起動以外はさりげなく（小さめ）
SPACE = {  # 名前 → (残りの響きの長さ, ディレイの量, リバーブの量)
    "boot": (1.0, 0.3, 0.45), "open": (0.24, 0.12, 0.16), "close": (0.24, 0.12, 0.16),
    "listen": (0.26, 0.1, 0.14), "ack": (0.3, 0.1, 0.12),
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
        if name in PAN:
            left, right = _pan(left, right, *PAN[name])
        paths[name] = _write(folder / f"{name}.wav", left, right, peak_db=PEAK_DB.get(name, -3.0))
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
