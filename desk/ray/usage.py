"""使える残り：Claude（Pro の枠）と Gemini（無料枠）の残りを、状態の画面に出す

- Claude：Claude Code がログインに使っている鍵（Mac のキーチェーン）で、Anthropic に今の使用率を聞く。
  /usage で見えるのと同じもの（5時間の枠と、1週間の枠）。回数ではなく「残り何％」で返ってくる。
  鍵は Anthropic にだけ送る。取れなければ画面には「不明」と出す（Ray の動きには影響しない）
- Gemini：Ray が数えている回数（GeminiBrain.count）と、上限（daily_limit）から出す
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
CREDENTIALS_FILE = Path.home() / ".claude" / ".credentials.json"


def _claude_token(run=subprocess.run) -> str | None:
    raw = ""
    try:
        raw = run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
                  capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    if not raw and CREDENTIALS_FILE.exists():
        raw = CREDENTIALS_FILE.read_text()
    try:
        return json.loads(raw)["claudeAiOauth"]["accessToken"]
    except (ValueError, KeyError, TypeError):
        return None


def _window(w: dict | None) -> dict | None:
    if not w or w.get("utilization") is None:
        return None
    used = float(w["utilization"])
    return {"left": max(0, round(100 - used)), "resets_at": w.get("resets_at")}


def claude_usage(urlopen=urllib.request.urlopen, token=_claude_token) -> dict:
    tok = token()
    if not tok:
        return {"ok": False, "why": "ログインの鍵が見つかりません"}
    req = urllib.request.Request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {tok}", "anthropic-beta": "oauth-2025-04-20", "User-Agent": "ray"})
    try:
        with urlopen(req, timeout=10) as r:
            data = json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"ok": False, "why": f"エラー {e.code}"}
    except (OSError, ValueError) as e:
        return {"ok": False, "why": str(e)[:60]}
    return {"ok": True, "five_hour": _window(data.get("five_hour")), "seven_day": _window(data.get("seven_day"))}


def gemini_usage(gemini) -> dict | None:
    if gemini is None:
        return None
    c = gemini.c
    if not c["enabled"]:
        return {"ok": False, "why": "使っていません"}
    if not gemini.key:
        return {"ok": False, "why": "鍵がありません"}
    gemini.available()  # 日が変わっていれば数を戻す
    limit = c["daily_limit"]
    left = 0 if gemini.exhausted_day == gemini.day else max(0, limit - gemini.count)
    return {"ok": True, "left": left, "limit": limit, "reset_hour": c["reset_hour"],
            "search": bool(c["search"]) and gemini.search_available(),
            "search_used": gemini.search_count, "search_limit": c.get("search_daily_limit", 0),
            "search_on": bool(c["search"])}


class UsageWatcher:
    """裏で残りを調べて、hub の usage に入れる。返事が終わるたび（待機中に戻ったとき）にも調べ直す"""

    def __init__(self, hub, gemini=None, claude_every: float = 300, every: float = 30,
                 fetch_claude=claude_usage, clock=time.monotonic):
        self.hub = hub
        self.gemini = gemini
        self.claude_every = claude_every
        self.every = every
        self.fetch_claude = fetch_claude
        self.clock = clock
        self.claude: dict = {"ok": False, "why": "調べています"}
        self.claude_at: float | None = None
        self.wake = threading.Event()

    def refresh(self, force_claude: bool = False) -> dict:
        now = self.clock()
        if force_claude or self.claude_at is None or now - self.claude_at >= self.claude_every:
            self.claude = self.fetch_claude()
            self.claude_at = now
        usage = {"claude": self.claude, "gemini": gemini_usage(self.gemini),
                 "checked": datetime.now().strftime("%H:%M")}
        self.hub.set(usage=usage)
        return usage

    def start(self) -> None:
        threading.Thread(target=self._loop, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def _loop(self) -> None:
        while True:
            try:
                self.refresh()
            except Exception as e:  # 残りの表示が壊れても、Ray は止めない
                print(f"使える残りを調べられませんでした：{e}")
            self.wake.wait(self.every)
            self.wake.clear()

    def _watch(self) -> None:
        """考えている／話している → 待機中 になったら、すぐ調べ直す（Claude は1分あけて）"""
        q = self.hub.subscribe()
        prev = None
        while True:
            state = q.get().get("state")
            if state in ("idle", "confirm") and prev in ("thinking", "speaking"):
                if self.claude_at is not None and self.clock() - self.claude_at >= 60:
                    self.claude_at = None
                self.wake.set()
            prev = state
