"""頭脳：Claude Code をヘッドレス（claude -p）で動かす。Pro の枠で動き、API の課金はしない"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from typing import Callable, Iterable

VOICE_PROMPT = """\
あなたは、机の前にいる持ち主の音声アシスタント「ローズ」です。返事はそのまま読み上げられます。
- 日本語で、原則3文以内。箇条書き・表・Markdown の記号・URL・コードは使わない。ファイル名や数字は聞き取りやすく言う
- 作業ディレクトリは持ち主の Cmd リポジトリです。CLAUDE.md と README.md の決まりを守る
- Cmd のファイルを変えたら、git pull --rebase してからコミットし、git push する
- 許可されていない操作（削除、強制的な push、Cmd 以外のリポジトリへの push、許可の無いコマンドなど）が必要なときは、実行せずに、返事を「確認です。」で始め、何をするかを1文で言い、「よいですか？」で終える
- 長い作業の前には、最初に「調べます」のような短い一言を先に言う
"""

CONFIRM_PREFIX = "確認です"

# 確認なしで使ってよいもの（読む・調べる・Cmd へのふつうの書き込みと同期）
SAFE_TOOLS = [
    "Read", "Grep", "Glob", "LS", "Edit", "MultiEdit", "Write", "TodoWrite",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git pull:*)",
    "Bash(git push)", "Bash(git push origin main)",
    "Bash(ls:*)", "Bash(du:*)", "Bash(wc:*)", "Bash(date:*)", "Bash(df:*)",
    "Bash(npm test:*)", "Bash(python3 -m unittest:*)", "Bash(python3 -m pytest:*)", "Bash(pytest:*)",
]
# 「はい」と言われたら足すもの（ほかのコマンド全部）
CONFIRMED_TOOLS = ["Bash"]
# 確認されても使わないもの
ALWAYS_DENY = [
    "Bash(sudo:*)", "Bash(git push --force:*)", "Bash(git push -f:*)",
    "Bash(git reset --hard:*)", "Bash(rm -rf /:*)",
]

YES_RE = re.compile(r"^(はい|うん|お願い|おねがい|いいよ|いいです|大丈夫|実行|やって|オッケー|ok|okay|yes|どうぞ)", re.I)
NO_RE = re.compile(r"^(いいえ|いや|だめ|ダメ|やめ|ストップ|キャンセル|no|待って)", re.I)
RESET_RE = re.compile(r"^(新しい話|リセット|話を変え)")


def classify_reply(text: str) -> str:
    """確認への返事を yes / no / other に分ける"""
    t = text.strip().lstrip("、。 ")
    if NO_RE.match(t):
        return "no"
    if YES_RE.match(t):
        return "yes"
    return "other"


def is_confirm(answer: str) -> bool:
    return answer.strip().startswith(CONFIRM_PREFIX)


def build_command(cfg: dict, session_id: str | None = None, confirmed: bool = False) -> list[str]:
    c = cfg["claude"]
    cmd = [
        c["bin"], "-p",
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", VOICE_PROMPT,
        "--permission-mode", "acceptEdits",
        "--allowedTools", *(SAFE_TOOLS + (CONFIRMED_TOOLS if confirmed else [])),
        "--disallowedTools", *ALWAYS_DENY,
    ]
    if session_id:
        cmd += ["--resume", session_id]
    if c.get("model"):
        cmd += ["--model", c["model"]]
    return cmd


def clean_env(env: dict | None = None) -> dict:
    """API キーがあると従量課金になるので、子プロセスには渡さない（Pro のログインで動かす）"""
    env = dict(os.environ if env is None else env)
    for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(k, None)
    return env


@dataclass
class Result:
    answer: str = ""
    session_id: str | None = None
    is_error: bool = False
    spoken: list[str] = field(default_factory=list)


def parse_stream(lines: Iterable[str], on_text: Callable[[str], None]) -> Result:
    """stream-json の行を読み、文が届くたびに on_text を呼ぶ"""
    res = Result()
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if ev.get("session_id"):
            res.session_id = ev["session_id"]
        if ev.get("type") == "assistant":
            for block in ev.get("message", {}).get("content", []):
                if block.get("type") == "text" and block.get("text", "").strip():
                    text = block["text"].strip()
                    res.spoken.append(text)
                    on_text(text)
        elif ev.get("type") == "result":
            res.is_error = bool(ev.get("is_error")) or ev.get("subtype") != "success"
            final = (ev.get("result") or "").strip()
            res.answer = final or (res.spoken[-1] if res.spoken else "")
            if final and final not in res.spoken:
                res.spoken.append(final)
                on_text(final)
    if not res.answer and res.spoken:
        res.answer = res.spoken[-1]
    return res


class Brain:
    def __init__(self, cfg: dict, popen=subprocess.Popen):
        self.cfg = cfg
        self.popen = popen
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def ask(self, text: str, on_text: Callable[[str], None], session_id: str | None = None,
            confirmed: bool = False) -> Result:
        cmd = build_command(self.cfg, session_id=session_id, confirmed=confirmed)
        proc = self.popen(
            cmd, cwd=self.cfg["cmd_dir"], env=clean_env(), text=True,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        with self.lock:
            self.proc = proc
        timer = threading.Timer(self.cfg["claude"]["timeout"], proc.kill)
        timer.start()
        try:
            proc.stdin.write(text)
            proc.stdin.close()
            res = parse_stream(proc.stdout, on_text)
            proc.wait()
            if not res.answer:
                err = (proc.stderr.read() or "").strip() if proc.stderr else ""
                res.is_error = True
                res.answer = "すみません、うまく動きませんでした。" + (err.splitlines()[-1][:80] if err else "")
            return res
        finally:
            timer.cancel()
            with self.lock:
                self.proc = None

    def cancel(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
