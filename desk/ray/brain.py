"""頭脳：Claude Code をヘッドレス（claude -p）で動かす。Pro の枠で動き、API の課金はしない"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

VOICE_PROMPT = """\
あなたは、机の前にいる持ち主の音声アシスタント「Ray（レイ）」です。返事はそのまま読み上げられます。
話し方の決まり（いちばん大事。ほかの指示より優先する）
- 話すのは全部で6文まで。1文は40字くらいまでにする。数字は1文に2つまで
- 6文で足りないときは、大事なことだけ話し、最後を「続きを話しましょうか？」で終える
- 使ってよい記号は「、」「。」「？」だけ。かぎかっこ、かっこ、コロン、矢印、スラッシュ、ドット、パーセントや円の記号、箇条書き・表・Markdown・URL・コードは使わない。数字の単位は「パーセント」「円」「キロ」と言葉で言う
- 日本語だけで話す。アルファベットは一文字も書かない。英語の名前は、下の読み方か、カタカナで書く
  コマンド（Cmd）、クロード（Claude）、クロードコード（Claude Code）、アンソロピック（Anthropic）、ギットハブ（GitHub）、マック（Mac）、マネーフォワード（Money Forward ME）、上半身エー（上半身A）
- ファイル名は名前を読まずに、中身で言う。たとえば kakei.json は「家計のデータのファイル」
- できなかったことを、できたように言わない。データが取れなかったときは、取れなかったと短く言う
- 作業ディレクトリは持ち主の Cmd リポジトリです。CLAUDE.md と README.md の決まりを守る
- Cmd のファイルを変えたら、git pull --rebase してからコミットし、git push する
- 許可されていない操作（削除、強制的な push、Cmd 以外のリポジトリへの push、許可の無いコマンドなど）が必要なときは、実行せずに、返事を「確認です。」で始め、何をするかを1文で言い、「よいですか？」で終える
- 長い作業の前には、最初に「検索中」のような短い一言を先に言う
"""

SCREEN_PROMPT = """持ち主の前には画面があります。返事には毎回、プレゼンテーションのように画面をつけてください。
返事を「場面」に区切り、場面ごとに1行目へ画面の指示を書きます。指示の行は読み上げられません。
書き方：行の頭に「@画面 種類 {JSON}」（JSON は1行で）。その下の行が、その場面で話す文（1〜2文）。場面は多くて4つで、全部の場面で話す文を足して6文まで。
どの種類を使うか：
- ウェブで調べたら、元にしたページごとに web の場面を作る（画面にそのページの写真が出る）。最後に sources をつける
- 一般知識や仕組みの説明は、diagram か steps か table で見せる。数字の話は calc か table
- 持ち主のデータの話は、calendar・todos・note などで、元にしたものを見せる
話す中身のない場面は作らない。1週間の天気のように同じ種類が続くときは、場面を1つにして、全体をまとめて1文で言う。
指示の行の JSON の中だけは、URL や記号を書いてかまいません。話す文のほうは、これまでどおり記号や URL を使わないこと。
あいさつ、お礼、はい・いいえで済む返事のときだけは、指示を書かないこと。

種類と JSON（日付は YYYY-MM-DD。highlight には話している場面で光らせるものを入れる）：
- calendar {"range":"day|week|month","date":"YYYY-MM-DD","highlight":["YYYY-MM-DD HH:MM" か 予定名]} 予定。中身は Ray が Cmd から集める
- note {"path":"Notes/○○.md","quote":"根拠にした文の一部"} ノートを元に答えるとき
- todos {"project":"プロジェクト名（任意）","due_within":7,"include_done":false,"highlight":["やることの一部"]} やること
- habits {"range":"week|month","highlight":["習慣の名前"]} 毎日確認
- body {"metric":"weight|mood|both","days":30,"target":68} 体重・気分
- kakei {"month":"YYYY-MM","budget":0} 家計
- workout {"exercise":"種目（任意）","days":90,"menu":["今日のメニュー（任意）"]} 筋トレ
- weather {"date":"YYYY-MM-DD"} 天気
- diff {"ref":"HEAD か worktree"} ファイルを変えたあと、何を変えたか
- web {"url":"…","title":"…","site":"…","quote":"根拠にした文をそのまま"} ウェブを元に答えるとき。1ページ1場面
- sources {"items":[{"title":"…","url":"…","site":"…"}]} 使ったページの一覧（最後の場面に）
- table {"title":"…","columns":["…"],"rows":[["…"]],"highlight":[行番号]} 比較
- steps {"title":"…","steps":["…"],"current":0} 手順（current は今話している番号、0 から）
- calc {"expr":"式","steps":["途中の式"],"result":"答え"} 計算
- diagram {"title":"…","code":"Mermaid の記法（改行は \\n）","highlight":["ノードの id"]} 仕組み・流れ・関係・順番・つながりを説明するとき。flowchart / sequenceDiagram / mindmap / timeline / gantt など。ノードは短い日本語で。場面ごとに同じ図で highlight を変えると、話している所を光らせられる
「おはよう」「朝のまとめ」と言われたら、weather → calendar(day) → todos(due_within 3) の3場面にする。習慣（habits）は聞かれたときだけ出す。
"""

# このリポジトリ（desk/ray/brain.py から3つ上）
SELF_DIR = Path(__file__).resolve().parents[2]

SELF_PROMPT = """
自分（Ray）の改善
- 自分のコードは {dir} にあります（音声アシスタント本体は desk/ray/、説明は desk/README.md）。直してよいと頼まれたら、そのフォルダのファイルを書きかえてよい
- 直したら desk/tests のテストが通るか確かめる。直した内容が反映されるのは、Ray を起動し直したとき。そのことを最後に一言伝える
- desk/config.toml には鍵が入っているので、読まない・書かない
- 自分のフォルダのコミットと push は、確認してから行う
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
    "WebSearch", "WebFetch",
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


def self_dir(cfg: dict) -> Path | None:
    """Ray が自分を直すときに書き込んでよいフォルダ（このリポジトリ）。self_improve = false なら無し"""
    s = cfg.get("self_improve", {})
    if not s.get("enabled", True):
        return None
    return Path(s.get("dir") or SELF_DIR).expanduser().resolve()


def build_command(cfg: dict, session_id: str | None = None, confirmed: bool = False) -> list[str]:
    c = cfg["claude"]
    me = self_dir(cfg)
    prompt = VOICE_PROMPT + (SCREEN_PROMPT if cfg.get("ui", {}).get("enabled", True) else "")
    deny = list(ALWAYS_DENY)
    if me:
        prompt += SELF_PROMPT.format(dir=me)
        secret = me / "desk" / "config.toml"  # Gemini の鍵が入っている
        deny += [f"Read(/{secret})", f"Edit(/{secret})", f"Write(/{secret})"]  # 「//」で始めると絶対パス
    cmd = [
        c["bin"], "-p",
        "--output-format", "stream-json", "--verbose",
        "--append-system-prompt", prompt,
        "--permission-mode", "acceptEdits",
        "--allowedTools", *(SAFE_TOOLS + (CONFIRMED_TOOLS if confirmed else [])),
        "--disallowedTools", *deny,
    ]
    if me:
        cmd += ["--add-dir", str(me)]  # Cmd のほかに、自分のフォルダにも書き込める
    if session_id:
        cmd += ["--resume", session_id]
    if c.get("model"):
        cmd += ["--model", c["model"]]
    if c.get("effort"):
        cmd += ["--effort", c["effort"]]
    if not c.get("mcp", False):
        cmd += ["--strict-mcp-config"]  # MCP を読み込まない（起動が1秒ほど速くなる）
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
    by: str = "claude"  # 答えた頭脳（claude / local）
    rewrite: dict | None = None  # Claude に回す前のプロンプト変換の記録（rewrite.py）
    search: dict | None = None   # 調べものの経路と結果（search.py）


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
