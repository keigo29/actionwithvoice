"""第二の脳：Codex CLI を非対話（codex exec）で動かす。コードの作業と、Claude が上限のときの受け皿

決まり（Cmd の Notes/音声連携.md「Codex を構成に含める」）
- Claude の置き換えではなく併用。コードの生成・修正・レビューと、一般的な開発の相談だけを渡す
- お金の詳細・仕事の機密・予定やメモは、これまでどおり Claude だけ（振り分けは local.py が決める）
- 最終回答は標準出力から取る（途中経過は標準エラーに出る）
- 作業するのは Ray のリポジトリだけ（-C）。Cmd は渡さない
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from typing import Callable

from .brain import SCREEN_LITE, Result, clean_env, self_dir

CODEX_PROMPT = """\
あなたは、机の前にいる持ち主の音声アシスタント「レイ」の、コードの係です。返事はそのまま読み上げられます。
話し方の決まり（いちばん大事。ほかの指示より優先する）
- 言葉づかいは「です・ます」の丁寧語。タメ口や「〜わ」「〜のよ」「〜だね」のようなくだけた語尾は使わない
- 話すのは3文まで。1文は40字くらいまで
- 記号は「、」「。」「？」だけ。かっこ、コロン、矢印、スラッシュ、バッククォート、箇条書き、Markdown、URL、コードは書かない
- 日本語だけで話す。アルファベットは書かない。名前はカタカナで言う（パイソン、ギット、テスト、関数など）
- コードを見せる代わりに、何をどう直したか・どうすればよいかを言葉で言う。手順や流れは画面に出す
- できなかったことを、できたように言わない
作業の決まり
- 作業してよいのは、今のフォルダ（Ray のリポジトリ）だけ。desk/config.toml には鍵が入っているので読まない
- ファイルを直したら、desk/tests のテストを流して確かめる。コミットと push はしない
- 直した内容が反映されるのは Ray を起動し直したとき。そのことを最後に一言伝える
"""

# Claude の5時間の上限（と週の上限）に当たったときの返事。英語のまま返ってくる
LIMIT_RE = re.compile(r"limit reached|hit your limit|usage limit|rate.?limit|out of (?:extra )?usage|上限に達", re.I)


def is_limit(res: Result) -> bool:
    text = " ".join([res.answer or "", *res.spoken])
    return bool(res.is_error and LIMIT_RE.search(text))


def build_command(cfg: dict) -> list[str]:
    c = cfg["codex"]
    me = self_dir(cfg)
    cmd = [c["bin"], "exec", "--skip-git-repo-check", "--color", "never", "--ephemeral",
           "-s", c.get("sandbox", "workspace-write")]
    if not c.get("user_config", False):
        # ~/.codex/config.toml を読まない（ほかの用途の既定モデルや MCP を持ち込まない。起動も速い）
        cmd.append("--ignore-user-config")
    if me:
        cmd += ["-C", str(me)]
    if c.get("model"):
        cmd += ["-m", c["model"]]
    if c.get("effort"):
        cmd += ["-c", f'model_reasoning_effort="{c["effort"]}"']
    return cmd + ["-"]  # 指示は標準入力から


class CodexBrain:
    def __init__(self, cfg: dict, popen=subprocess.Popen):
        self.cfg = cfg
        self.popen = popen
        self.proc: subprocess.Popen | None = None
        self.lock = threading.Lock()

    def ask(self, text: str, on_text: Callable[[str], None], history: list[tuple[str, str]] | None = None) -> Result:
        lines = "\n".join(f"持ち主：{q}\nレイ：{a}" for q, a in (history or []))
        screen = SCREEN_LITE if self.cfg.get("ui", {}).get("enabled", True) else ""
        prompt = CODEX_PROMPT + screen + (f"\n（ここまでのやり取り）\n{lines}\n" if lines else "") + f"\n（今の頼み）\n{text}\n"
        started = time.monotonic()
        try:
            proc = self.popen(build_command(self.cfg), cwd=str(self_dir(self.cfg) or self.cfg["cmd_dir"]),
                              env=clean_env(), text=True,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except OSError as e:
            return Result(answer=f"コーデックスを起動できませんでした。{e}", is_error=True, by="codex")
        with self.lock:
            self.proc = proc
        timer = threading.Timer(self.cfg["codex"]["timeout"], proc.kill)
        timer.start()
        try:
            out, err = proc.communicate(prompt)
        finally:
            timer.cancel()
            with self.lock:
                self.proc = None
        answer = (out or "").strip()
        usage = {"prompt_chars": len(prompt), "wall_ms": int((time.monotonic() - started) * 1000)}
        m = re.search(r"tokens used\s*\n\s*([\d,]+)", err or "")
        if m:
            usage["tokens"] = int(m.group(1).replace(",", ""))
        if proc.returncode != 0 or not answer:
            last = next((ln for ln in reversed((err or "").splitlines()) if "ERROR" in ln), "")
            print(f"Codex が答えませんでした（{proc.returncode}）：{last[:120]}")
            return Result(answer="", is_error=True, by="codex", usage=usage)
        on_text(answer)
        return Result(answer=answer, spoken=[answer], by="codex", usage=usage)

    def cancel(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                self.proc.kill()
