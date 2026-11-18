"""やり取りを Cmd の Data/voice/ に Q&A の JSON として置く（Cmd の Actions がデイリーに追記する）"""

from __future__ import annotations

import json
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

JST = timezone(timedelta(hours=9))
_lock = threading.Lock()


def write_qa(cmd_dir: str | Path, question: str, answer: str, now: datetime | None = None,
             extra: dict | None = None) -> Path:
    now = (now or datetime.now(JST)).astimezone(JST)
    folder = Path(cmd_dir) / "Data" / "voice"
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    path = folder / f"{stamp}.json"
    n = 2
    while path.exists():
        path = folder / f"{stamp}-{n}.json"
        n += 1
    record = {"datetime": now.isoformat(timespec="seconds"), "question": question, "answer": answer, "via": "desk"}
    # raw：画面の指示行も含めた Claude の返事そのまま。trimmed：3文を超えて途中で止めた
    record.update({k: v for k, v in (extra or {}).items() if v})
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def push(cmd_dir: str | Path, path: Path, run=subprocess.run) -> bool:
    """その1ファイルだけをコミットして push する（ほかの変更は巻き込まない）"""
    cwd = str(cmd_dir)
    rel = str(Path(path).relative_to(cwd))
    with _lock:
        steps = [
            ["git", "add", "--", rel],
            ["git", "commit", "-q", "-m", f"音声Q&A（デスク） {Path(path).stem}", "--", rel],
        ]
        for s in steps:
            if run(s, cwd=cwd, capture_output=True, text=True).returncode != 0:
                return False
        for _ in range(3):
            pulled = run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=cwd, capture_output=True, text=True)
            if pulled.returncode == 0 and run(["git", "push", "-q"], cwd=cwd, capture_output=True, text=True).returncode == 0:
                return True
        return False


def log_async(cfg: dict, question: str, answer: str, extra: dict | None = None) -> None:
    if not cfg["log"]["enabled"]:
        return

    def work():
        try:
            p = write_qa(cfg["cmd_dir"], question, answer, extra=extra)
            if cfg["log"]["push"] and not push(cfg["cmd_dir"], p):
                print(f"ログの push に失敗しました（あとで Cmd で push してください）：{p.name}")
        except Exception as e:  # ログの失敗で会話は止めない
            print(f"ログを残せませんでした：{e}")

    threading.Thread(target=work, daemon=True).start()
