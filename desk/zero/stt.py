"""音声入力：手元の Whisper（mlx-whisper か whisper.cpp）で文字にする。無料・オフライン"""

from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
from pathlib import Path

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

        out = mlx_whisper.transcribe(str(wav_path), path_or_hf_repo=s["mlx_model"], language=s["language"])
        return clean_transcript(out.get("text", ""))
    if engine == "whisper_cpp":
        p = run(
            [s["whisper_cpp_bin"], "-m", s["whisper_cpp_model"], "-l", s["language"], "-nt", "-np", "-f", str(wav_path)],
            capture_output=True, text=True, check=False,
        )
        if p.returncode != 0:
            raise RuntimeError(f"whisper.cpp が失敗しました：{p.stderr.strip()[-200:]}")
        return clean_transcript(p.stdout)
    raise RuntimeError(f"知らない音声入力エンジンです：{engine}")
