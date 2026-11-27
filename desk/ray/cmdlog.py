"""やり取りを Cmd の Data/ray/ に Q&A の JSON として置く

デイリーには入れない（Data/voice/ に置くと Cmd の Actions がデイリーの ## メモ に追記して、
やることや予定が埋もれてしまうため。Data/voice/ は iPhone の「聞く」だけが使う）。
"""

from __future__ import annotations

import atexit
import json
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

JST = timezone(timedelta(hours=9))
_lock = threading.Lock()


LOG_DIR = "Data/ray"
PUSH_DELAY = 60  # 最後に書いてから push するまでの秒数（[log] push_delay）


def write_qa(cmd_dir: str | Path, question: str, answer: str, now: datetime | None = None,
             extra: dict | None = None, folder: str = LOG_DIR) -> Path:
    now = (now or datetime.now(JST)).astimezone(JST)
    folder = Path(cmd_dir) / folder
    folder.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    path = folder / f"{stamp}.json"
    n = 2
    while path.exists():
        path = folder / f"{stamp}-{n}.json"
        n += 1
    record = {"datetime": now.isoformat(timespec="seconds"), "question": question, "answer": answer, "via": "desk"}
    # raw：画面の指示行も含めた Claude の返事そのまま。trimmed：決まった文の数を超えて途中で止めた
    record.update({k: v for k, v in (extra or {}).items() if v})
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def commit(cmd_dir: str | Path, path: Path, run=subprocess.run, message: str = "", tries: int = 3,
           sleep=time.sleep) -> bool:
    """その1ファイルだけをコミットする（ほかの変更は巻き込まない）。Claude が同じ時に git を使っていたら、少し待ってやり直す"""
    cwd = str(cmd_dir)
    rel = str(Path(path).relative_to(cwd))
    for i in range(tries):
        with _lock:
            if (run(["git", "add", "--", rel], cwd=cwd, capture_output=True, text=True).returncode == 0 and
                    run(["git", "commit", "-q", "-m", message or f"Ray との会話 {Path(path).stem}", "--", rel],
                        cwd=cwd, capture_output=True, text=True).returncode == 0):
                return True
        if i + 1 < tries:
            sleep(0.5 * (i + 1))  # index.lock（ほかの git が動いている）なら、終わるのを待つ
    return False


def push_now(cmd_dir: str | Path, run=subprocess.run) -> bool:
    """ためたコミットを送る。先に最新を取り込み、ぶつかって止まったら元に戻す"""
    cwd = str(cmd_dir)
    with _lock:
        for _ in range(3):
            pulled = run(["git", "pull", "-q", "--rebase", "--autostash"], cwd=cwd, capture_output=True, text=True)
            if pulled.returncode != 0:
                run(["git", "rebase", "--abort"], cwd=cwd, capture_output=True, text=True)
                continue
            if run(["git", "push", "-q"], cwd=cwd, capture_output=True, text=True).returncode == 0:
                return True
        return False


def push(cmd_dir: str | Path, path: Path, run=subprocess.run, message: str = "") -> bool:
    """その1ファイルだけをコミットして、すぐ push する"""
    return commit(cmd_dir, path, run=run, message=message) and push_now(cmd_dir, run=run)


class Syncer:
    """push をまとめる。コミットはすぐ、push は最後の書き込みから delay 秒あと（会話の途中なら終わるまで待つ）

    会話ごとに pull と push をすると、Claude が Cmd で git を使うのとぶつかりやすく、ネットも使うので、まとめて1回にする。
    Ray を止めるときは、残りをすぐ送る。
    """

    def __init__(self, run=subprocess.run, timer=threading.Timer):
        self.run, self.timer = run, timer
        self.busy: Callable[[], bool] = lambda: False  # 会話の途中か（app が渡す）
        self.dirs: dict[str, float] = {}
        self.t = None
        self.lock = threading.Lock()
        atexit.register(self.flush, force=True)

    def save(self, cfg: dict, path: Path, message: str = "") -> bool:
        """コミットして、push を予約する。delay が 0 ならすぐ送る"""
        root = str(cfg["cmd_dir"])
        if not commit(root, path, run=self.run, message=message):
            return False
        delay = float(cfg["log"].get("push_delay", PUSH_DELAY))
        if delay <= 0:
            return push_now(root, run=self.run)
        with self.lock:
            self.dirs[root] = delay
            self._arm(delay)
        return True

    def _arm(self, delay: float) -> None:
        if self.t:
            self.t.cancel()
        self.t = self.timer(delay, self.flush)
        self.t.daemon = True
        self.t.start()

    def flush(self, force: bool = False) -> None:
        with self.lock:
            if not self.dirs:
                return
            if not force and self.busy():
                self._arm(max(self.dirs.values()) / 2)  # 会話が終わってから送る
                return
            dirs, self.dirs, self.t = list(self.dirs), {}, None
        for d in dirs:
            if not push_now(d, run=self.run):
                print("Cmd の push に失敗しました（次に書いたとき、もう一度送ります）")


SYNC = Syncer()


def log_async(cfg: dict, question: str, answer: str, extra: dict | None = None) -> None:
    if not cfg["log"]["enabled"]:
        return

    def work():
        try:
            p = write_qa(cfg["cmd_dir"], question, answer, extra=extra, folder=cfg["log"]["dir"])
            if cfg["log"]["push"] and not SYNC.save(cfg, p):
                print(f"ログをコミットできませんでした（あとで Cmd で push してください）：{p.name}")
        except Exception as e:  # ログの失敗で会話は止めない
            print(f"ログを残せませんでした：{e}")

    threading.Thread(target=work, daemon=True).start()
