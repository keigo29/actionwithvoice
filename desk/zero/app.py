"""会話の流れ：聞き取り → 文字にする → Claude Code → 読み上げ → Cmd にログ"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from . import cmdlog, stt
from .audio import MIN_SEC, duration, save_wav
from .brain import RESET_RE, classify_reply, is_confirm

CONFIRMED_PROMPT = "はい、さきほど確認した操作を実行してください。終わったら結果を短く教えてください。"


class Assistant:
    def __init__(self, cfg, brain, speaker, hub, log=cmdlog.log_async, transcribe=stt.transcribe,
                 clock=time.monotonic):
        self.cfg = cfg
        self.brain = brain
        self.speaker = speaker
        self.hub = hub
        self.log = log
        self.transcribe = transcribe
        self.clock = clock
        self.session_id: str | None = None
        self.last = -1e9
        self.awaiting_confirm = False
        self.worker: threading.Thread | None = None
        self.busy = threading.Event()

    # --- 割り込み（キーを押した・呼ばれた） ---
    def interrupt(self) -> None:
        """話している途中・考えている途中でも、すぐ止めて次を聞く"""
        self.speaker.stop()
        self.brain.cancel()
        self.hub.set(state="listening", text="聞いています…")

    def submit_audio(self, frames: bytes) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
        self.worker = threading.Thread(target=self.handle_audio, args=(frames,), daemon=True)
        self.worker.start()

    # --- 中身 ---
    def handle_audio(self, frames: bytes) -> None:
        if duration(frames) < MIN_SEC:
            self.hub.set(state="idle", text="")
            return
        self.busy.set()
        try:
            self.hub.set(state="thinking", text="聞き取っています…")
            with tempfile.TemporaryDirectory() as d:
                wav = save_wav(Path(d) / "q.wav", frames)
                try:
                    text = self.transcribe(wav, self.cfg)
                except Exception as e:
                    print(f"音声入力のエラー：{e}")
                    self.speaker.say("聞き取りでエラーが出ました")
                    self.hub.set(state="idle", text="")
                    return
            self.handle_text(text)
        finally:
            self.busy.clear()

    def handle_text(self, question: str) -> str | None:
        question = question.strip()
        if not question:
            self.speaker.say("聞き取れませんでした")
            self.hub.set(state="idle", text="")
            return None
        print(f"🎙 {question}")

        if RESET_RE.match(question):
            self.session_id, self.awaiting_confirm = None, False
            return self._local(question, "新しい話にしますね")

        confirmed = False
        prompt = question
        if self.awaiting_confirm:
            self.awaiting_confirm = False
            kind = classify_reply(question)
            if kind == "no":
                return self._local(question, "やめておきますね")
            if kind == "yes":
                confirmed, prompt = True, CONFIRMED_PROMPT

        fresh = self.clock() - self.last < self.cfg["conversation_timeout"]
        session = self.session_id if (fresh or confirmed) else None
        self.hub.set(state="thinking", question=question, text="考えています…")

        def on_text(t: str) -> None:
            print(f"🔊 {t}")
            self.hub.set(state="speaking", text=t)
            self.speaker.say(t)

        res = self.brain.ask(prompt, on_text, session_id=session, confirmed=confirmed)
        if res.session_id:
            self.session_id = res.session_id
        self.last = self.clock()
        if res.is_error and not res.spoken:
            on_text(res.answer)
        self.awaiting_confirm = (not res.is_error) and is_confirm(res.answer)
        self.log(self.cfg, question, res.answer)
        self.speaker.wait(timeout=120)
        self.hub.set(state="confirm" if self.awaiting_confirm else "idle", answer=res.answer, text="")
        return res.answer

    def _local(self, question: str, answer: str) -> str:
        """Claude を呼ばずに返す返事"""
        print(f"🔊 {answer}")
        self.speaker.say(answer)
        self.log(self.cfg, question, answer)
        self.hub.set(state="idle", question=question, answer=answer, text="")
        return answer
