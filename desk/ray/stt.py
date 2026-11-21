"""音声入力：手元の Whisper（mlx-whisper か whisper.cpp）で文字にする。無料・オフライン"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import threading
from pathlib import Path

_LOCK = threading.Lock()  # 下準備と最初の聞き取りが重なっても、モデルは1つずつ使う

# 無音や雑音のときに Whisper が出しがちな決まり文句
HALLUCINATIONS = [
    "ご視聴ありがとうございました", "ご清聴ありがとうございました", "チャンネル登録",
    "お疲れ様でした", "字幕", "おやすみなさい",
]


def clean_transcript(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)", "", text)  # [音楽] (拍手) など
    text = re.sub(r"\s+", " ", text).strip()
    bare = re.sub(r"[。、！？!?.\s]", "", text)
    if not bare:
        return ""
    if any(bare == re.sub(r"[。、！？!?.\s]", "", h) for h in HALLUCINATIONS):
        return ""
    return text


def _bare(text: str) -> str:
    return re.sub(r"[。、！？!?.,\s]", "", text)


def echoes_prompt(text: str, prompt: str) -> bool:
    """ヒントの言葉をそのまま返しただけか（無音のとき Whisper はヒントを読み上げがち）。1語だけなら本当の発話として残す"""
    t = _bare(text)
    return bool(prompt) and len(t) >= 8 and t in _bare(prompt)


def pick_engine(cfg: dict) -> str:
    engine = cfg["stt"]["engine"]
    if engine != "auto":
        return engine
    if importlib.util.find_spec("mlx_whisper"):
        return "mlx"
    if shutil.which(cfg["stt"]["whisper_cpp_bin"]):
        return "whisper_cpp"
    raise RuntimeError("音声入力のエンジンがありません（mlx-whisper か whisper.cpp を入れてください。README 参照）")


def transcribe(wav_path: str | Path, cfg: dict, run=subprocess.run) -> str:
    s = cfg["stt"]
    engine = pick_engine(cfg)
    if engine == "mlx":
        import mlx_whisper  # Apple Silicon 用

        with _LOCK:
            out = mlx_whisper.transcribe(str(wav_path), path_or_hf_repo=s["mlx_model"], language=s["language"],
                                         initial_prompt=s.get("prompt") or None)
        return _drop_echo(clean_transcript(out.get("text", "")), s)
    if engine == "whisper_cpp":
        p = run(
            [s["whisper_cpp_bin"], "-m", s["whisper_cpp_model"], "-l", s["language"], "-nt", "-np", "-f", str(wav_path)]
            + (["--prompt", s["prompt"]] if s.get("prompt") else []),
            capture_output=True, text=True, check=False,
        )
        if p.returncode != 0:
            raise RuntimeError(f"whisper.cpp が失敗しました：{p.stderr.strip()[-200:]}")
        return _drop_echo(clean_transcript(p.stdout), s)
    raise RuntimeError(f"知らない音声入力エンジンです：{engine}")


def _drop_echo(text: str, s: dict) -> str:
    return "" if echoes_prompt(text, s.get("prompt", "")) else text


def warm(cfg: dict, transcribe_fn=None) -> None:
    """起動したら、無音を一度文字にしてモデルを読み込んでおく（最初の1回の聞き取りが8秒かかっていた）"""
    import tempfile

    from .audio import RATE, save_wav

    try:
        with tempfile.TemporaryDirectory() as d:
            wav = save_wav(Path(d) / "warm.wav", b"\x00\x00" * (RATE // 2))
            (transcribe_fn or transcribe)(wav, cfg)
    except Exception as e:  # 読み込めなくても、最初の聞き取りで読み込むだけ
        print(f"聞き取りの下準備ができませんでした：{e}")
