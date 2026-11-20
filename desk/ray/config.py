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
    # 返事が6文・決まった場面の数を超えても、「続き」「次」と言わずに最後まで話して見せる
    # （途中で止めたいときはキーを押す）。false で、6文で止めて「続き」を待つ
    "auto_continue": True,
    # 自己改善：頼まれたら Ray が自分のコード（このリポジトリ）を書きかえてよい。
    # dir を空にすると、このリポジトリ。コミット・push は確認してから。desk/config.toml は読み書きさせない
    "self_improve": {"enabled": True, "dir": ""},
    "stt": {
        # auto / mlx / whisper_cpp
        "engine": "auto",
        "mlx_model": "mlx-community/whisper-large-v3-turbo",
        "whisper_cpp_bin": "whisper-cli",
        "whisper_cpp_model": "~/models/ggml-medium.bin",
        "language": "ja",
    },
    # lipsync：声をいったん書き出して大きさを測り、画面の円の内側の光を発音に合わせて光らせる（false で say に直接）
    # engine：say（Mac の声）／voicevox（VOICEVOX のアプリを入れて使う。起動していなければ Ray が起動し、準備できるまでは say）
    "tts": {"voice": "Kyoko", "rate": 210, "lipsync": True, "engine": "say",
            "voicevox": {"url": "http://127.0.0.1:50021", "speaker": 3, "speed": 1.15, "app": "VOICEVOX", "timeout": 20}},
    # 返事の速さ優先：Sonnet・考える量は少なめ・MCP（Discord や Google Drive など）は読み込まない
    # （Opus の既定より、ひとことで 4.4→2.3 秒、デイリーを読むので 9.2→4.7 秒に縮んだ）
    "claude": {"bin": "claude", "model": "sonnet", "effort": "low", "mcp": False, "timeout": 180},
    # 一次受けのローカル LLM（Ollama）。答えられないもの・止まっているときは Claude に回す
    "local": {
        "enabled": True,
        "url": "http://localhost:11434",
        "model": "qwen2.5:7b",
        # 載せておく時間。-1 は Ray が動いている間ずっと（止めるときに外す）。"1h" などにすると、
        # 外れたあとの次の読み込みに30秒ほどかかる（その間は Claude）
        "keep_alive": -1,
        "timeout": 8,           # この秒数、何も返ってこなければ Claude に回す
        "temperature": 0.3,
        "num_ctx": 4096,
        "history": 6,           # 覚えておく直前のやり取りの数
    },
    # Gemini の無料枠（有料枠は使わない。鍵は課金を設定していないプロジェクトのもの）
    # 鍵は環境変数 GEMINI_API_KEY か、config.toml の api_key（config.toml は Git に入らない）
    "gemini": {
        "enabled": True,
        "api_key": "",
        "model": "gemini-flash-lite-latest",  # Flash-Lite（1日500回）。Flash（1日20回）は温存する
        "search": True,          # 調べものも Google 検索つきで Gemini に任せる
        "search_daily_limit": 0, # 検索つきの1日の上限（別の枠）。分かったら書く。0 は「不明」（上限のエラーで止める）
        "max_sources": 5,        # 調べたときに画面に出すページの数
        "daily_limit": 450,      # 念のため、無料枠（500回）より少し手前で止める
        "reset_hour": 17,        # 無料枠が日本時間の何時に戻るか
        "timeout": 15,
        "temperature": 0.3,
        "history": 6,
    },
    # 検索の受け皿（search.py）：検索 API（Tavily）で取り → Gemini 無料枠で要約 → だめなら Claude
    # 鍵は環境変数 TAVILY_API_KEY か、config.toml の api_key（config.toml は Git に入らない）
    "search": {
        "enabled": True,
        "provider": "tavily",
        "api_key": "",
        "depth": "basic",        # basic は1クレジット（advanced は2）
        "max_results": 5,
        "summary_results": 5,    # 要約に渡す上位の数
        "country": "japan",      # 日本のページを優先する
        "near": "",              # 「近く」をこの場所の名前に置き換える（空なら [weather] place）。検索の言葉として外に出る
        "monthly_limit": 950,    # 念のため、無料枠（月1,000クレジット）より少し手前で止める
        "timeout": 10,
    },
    "wake": {
        "enabled": False,
        # openWakeWord のモデル（.onnx / .tflite）。「レイ」は自分で学習させたものを指定
        "model": "",
        "threshold": 0.5,
        # 呼ばれたあと、この秒数黙ったら聞き取りを終える
        "silence_sec": 1.2,
        "max_sec": 15,
    },
    # usage：Claude（Pro の枠）と Gemini（無料枠）の残りを画面の隅に出す
    # theme：画面の見た目。neon（暗い背景・オレンジの光）／blueprint（明るい設計図風）
    # browser：起動時に画面を開くブラウザ（Mac のアプリ名。"Safari"・"Google Chrome" など）。"" ならふだん使うブラウザ
    "ui": {"enabled": True, "port": 8765, "open_browser": True, "usage": True, "theme": "neon", "browser": "Safari"},
    # 天気の場所（open-meteo）
    "weather": {"place": "横浜", "latitude": 35.44, "longitude": 139.64},
    # 会話の記録。Data/voice/ にすると Cmd の Actions がデイリーに書き足すので、別の場所に置く
    "log": {"enabled": True, "push": True, "dir": "Data/ray"},
    # 効果音（起動・画面を開く／閉じる・聞き取り開始・聞き取れた）。Mac の afplay で鳴らす
    "sfx": {"enabled": True, "volume": 0.5, "pitch": 0.5},
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
