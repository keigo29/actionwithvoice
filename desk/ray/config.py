"""設定の読み込み（config.toml。無ければ既定値）"""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path

DEFAULTS: dict = {
    # Claude Code が作業するフォルダ（Cmd のローカルクローン）
    "cmd_dir": "~/Cmd",
    # 押している間だけ聞き取るキー（pynput の Key の名前か1文字）
    "hotkey": "alt_r",
    # 最後のやり取りからこの秒数以内なら、前の話の続きとして聞く
    "conversation_timeout": 300,
    "stt": {
        # auto / mlx / whisper_cpp
        "engine": "auto",
        "mlx_model": "mlx-community/whisper-large-v3-turbo",
        "whisper_cpp_bin": "whisper-cli",
        "whisper_cpp_model": "~/models/ggml-medium.bin",
        "language": "ja",
    },
    "tts": {"voice": "Kyoko", "rate": 210},
    "claude": {"bin": "claude", "model": "", "timeout": 180},
    "wake": {
        "enabled": False,
        # openWakeWord のモデル（.onnx / .tflite）。「レイ」は自分で学習させたものを指定
        "model": "",
        "threshold": 0.5,
        # 呼ばれたあと、この秒数黙ったら聞き取りを終える
        "silence_sec": 1.2,
        "max_sec": 15,
    },
    "ui": {"enabled": True, "port": 8765, "open_browser": True},
    "log": {"enabled": True, "push": True},
}


def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load(path: str | Path | None = None) -> dict:
    cfg = copy.deepcopy(DEFAULTS)
    if path is None:
        path = Path(__file__).resolve().parent.parent / "config.toml"
    path = Path(path).expanduser()
    if path.exists():
        with path.open("rb") as f:
            cfg = _merge(cfg, tomllib.load(f))
    cfg["cmd_dir"] = str(Path(cfg["cmd_dir"]).expanduser())
    for key in ("whisper_cpp_model",):
        cfg["stt"][key] = str(Path(cfg["stt"][key]).expanduser())
    if cfg["wake"]["model"]:
        cfg["wake"]["model"] = str(Path(cfg["wake"]["model"]).expanduser())
    return cfg
