"""会話の流れ：聞き取り → 文字にする → Claude Code → 読み上げ → Cmd にログ"""

from __future__ import annotations

import queue
import tempfile
import threading
import time
from pathlib import Path

import re

from . import canned, cmdlog, guard, scenes, shots, speech, stt, visuals
from .audio import MIN_SEC, duration, save_wav
from .brain import RESET_RE, Result, classify_reply, is_confirm

SOURCE_SHOTS = 3  # 調べたページの一覧で写真を撮る数（1件 数秒かかるので上位だけ）

CONFIRMED_PROMPT = "はい、さきほど確認した操作を実行してください。終わったら結果を短く教えてください。"
MORE_SCENES = "続きは、次と言ってください。"
MORE_TEXT = "続きを聞くときは、続きと言ってください。"
NOTICE_POLL = 0.3  # 知らせを待たせているとき、会話が終わったかを見る間隔（秒）

# 画面を見せながら話しているとき（話し終えたあとも）、Claude を呼ばずに扱う言葉
_END = r"[。.!！]?$"
NEXT_RE = re.compile(r"^(次|つぎ|次へ|次の|進んで|続けて|続き)" + _END)
BACK_RE = re.compile(r"^(戻って|もどって|前|まえ|前の|一つ前|ひとつ前)" + _END)
AGAIN_RE = re.compile(r"^(もう一回|もう一度|もういっかい|最初から)" + _END)
BIG_RE = re.compile(r"^(大きく|おおきく|拡大)" + _END)
SMALL_RE = re.compile(r"^(小さく|ちいさく|戻して|元に戻して)" + _END)
CLOSE_RE = re.compile(r"^(閉じて|画面を閉じて|画面を消して)" + _END)


def asks_more(answer: str) -> bool:
    """返事が「続きを話しましょうか？」で終わっているか"""
    return bool(re.search(r"続き[をは].{0,6}[？?]\s*$", answer.strip()))


class _NoSfx:
    def play(self, name: str, wait: float = 0.0) -> None:
        pass


class Assistant:
    def __init__(self, cfg, brain, speaker, hub, log=cmdlog.log_async, transcribe=stt.transcribe,
                 clock=time.monotonic, sfx=None):
        self.cfg = cfg
        self.sfx = sfx or _NoSfx()
        self.on_screen: str | None = None    # 今出している画面の種類（効果音を鳴らすか決める）
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
        self.deck: list[scenes.Scene] = []  # 今の返事の場面
        self.pos = -1                        # 今見せている場面
        self.cut = threading.Event()         # 割り込まれた（キーを押した）
        self.gen = 0                         # キーを押すたびに増やす。聞き取りの途中で次が来たら、前の分は捨てる
        self.rest: list[str] = []            # 決まった文の数を超えて、まだ話していない文
        self.last_qa: tuple[str, str] | None = None  # 直前の質問と、画面の指示つきの答え（定番に保存する用）
        self.shot = shots.capture
        self.notices: queue.Queue = queue.Queue()  # n8n などから来た知らせ。会話が終わってから話す
        self.notice_thread: threading.Thread | None = None
        self.follow = None  # ボタンなしで聞く関数（should_stop を受けて、録った音を返す）。None なら使わない

    # --- 割り込み（キーを押した・呼ばれた） ---
    def interrupt(self) -> None:
        """話している途中・考えている途中でも、すぐ止めて次を聞く"""
        self.gen += 1
        self.cut.set()
        self.speaker.stop()
        self.sfx.play("listen")
        self.brain.cancel()
        self.hub.set(state="listening", text="聞いています…")

    def submit_audio(self, frames: bytes) -> None:
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)
        self.worker = threading.Thread(target=self.handle_audio, args=(frames, self.gen), daemon=True)
        self.worker.start()

    # --- 外からの知らせ（n8n など） ---
    def notify(self, text: str, title: str = "") -> int:
        """知らせを順番待ちに入れる。待っている数を返す"""
        self.notices.put((text, title, self.clock()))
        if not (self.notice_thread and self.notice_thread.is_alive()):
            self.notice_thread = threading.Thread(target=self._notice_loop, daemon=True)
            self.notice_thread.start()
        return self.notices.qsize()

    def idle(self) -> bool:
        """話しかけられていない・答えていない（確認を待っているだけなら、知らせてよい）"""
        working = self.worker is not None and self.worker.is_alive()
        return not self.busy.is_set() and not working and self.hub.state.get("state") in ("idle", "confirm")

    def _notice_loop(self) -> None:
        limit = self.cfg.get("notify", {}).get("wait_sec", 600)
        while True:
            try:
                text, title, at = self.notices.get(timeout=5)
            except queue.Empty:
                return
            while not self.idle() and self.clock() - at < limit:
                time.sleep(NOTICE_POLL)
            if not self.idle():
                print(f"（知らせを捨てました。会話が{limit}秒より長く続いたため：{title or text[:20]}）")
                continue
            try:
                self.announce(text, title)
            except Exception as e:  # 知らせ1件の失敗で、受け口ごと止めない
                print(f"知らせのエラー：{e}")

    def announce(self, text: str, title: str = "") -> None:
        """知らせを話す。「@画面」の行があれば画面も出す。Claude も Gemini も呼ばない"""
        self.busy.set()
        try:
            self.cut.clear()
            label = f"n8n：{title}" if title else "n8n から"
            print(f"📨 {label}")
            self.sfx.play("ack")
            budget = guard.Budget()
            self.hub.set(state="speaking", question=label, text="", **({} if self.awaiting_confirm else {"scene": None}))
            if scenes.has_directive(text):
                parsed = scenes.parse(text)
                for sc in parsed:
                    sc.speech = guard.to_japanese(guard.drop_english(sc.speech))
                    self._prepare(sc)
                self.deck, self.on_screen = parsed, None
                self._play(0, budget=budget)
            else:
                said = budget.take(speech.clean_for_speech(text))
                if said:
                    print(f"🔊 {said}")
                    self.hub.set(state="speaking", text=said)
                    self.speaker.say(said)
            if budget.overflow and not self.cut.is_set():
                if self.deck and self.pos < len(self.deck) - 1:
                    self.speaker.say(MORE_SCENES)
                else:
                    self.rest = budget.rest
                    self.speaker.say(MORE_TEXT)
            answer = scenes.strip_directives(text) or text
            self.log(self.cfg, label, answer, extra={"brain": "n8n"})
            self.speaker.wait(timeout=120)
            if self.cut.is_set():
                return  # 途中で話しかけられた：状態はそちらに任せる
            extra = {}
            if self.awaiting_confirm:
                extra["scene"] = {"kind": "confirm", "data": {"text": self.hub.state.get("answer", "")}, "index": 0, "total": 1}
            self.hub.set(state="confirm" if self.awaiting_confirm else "idle", answer=answer, text="", **extra)
        finally:
            self.busy.clear()

    # --- 中身 ---
    def handle_audio(self, frames: bytes, gen: int | None = None) -> None:
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
            if gen is not None and gen != self.gen:
                # 聞き取りの途中で、もう次のキーが押された：この分は答えない（2つ同時に Claude が動くのを防ぐ）
                print(f"（前の聞き取りは捨てました：{text.strip()}）")
                return
            if text.strip():
                self.sfx.play("ack")  # 聞き取れた。考え始める合図
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

        self.cut.clear()
        if RESET_RE.match(question):
            self.session_id, self.awaiting_confirm = None, False
            self.deck, self.pos = [], -1
            self._screen(None)
            self.hub.set(scene=None)
            return self._local(question, "新しい話にしますね")
        if not self.awaiting_confirm and self._navigate(question):
            return "（画面の操作）"
        if not self.awaiting_confirm and canned.is_save(question):
            return self._save_canned(question)
        self.rest = []

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
        self.hub.set(state="thinking", question=question, text="考えています…", scene=None)
        self.on_screen = None  # 新しい返事の画面は、同じ種類でも開く音を鳴らす
        new_deck: list[scenes.Scene] = []
        # 1つの返事で話すのは6文まで。auto_continue なら止めずに最後まで
        budget = guard.Budget(limit=10**6) if self.cfg.get("auto_continue", True) else guard.Budget()

        def on_text(t: str) -> None:
            if self.cut.is_set():
                return
            if scenes.has_directive(t):
                parsed = scenes.parse(t)
                start = len(new_deck)
                for sc in parsed:
                    sc.speech = guard.to_japanese(guard.drop_english(sc.speech))
                    self._prepare(sc)
                new_deck.extend(parsed)
                self.deck = new_deck
                self._play(start, budget=budget)
                return
            said = budget.take(speech.clean_for_speech(guard.drop_english(t)))
            if not said:
                return
            print(f"🔊 {said}")
            self.hub.set(state="speaking", text=said)
            self.speaker.say(said)

        hit = None if (self.awaiting_confirm or confirmed) else canned.find(question, canned.dirs_for(self.cfg))
        if hit:
            # 定番の返事：用意しておいたものをそのまま流す（Claude も Gemini も呼ばない）
            print(f"（定番の返事を使いました：{hit.name}）")
            on_text(hit.body)
            res = Result(answer=hit.body, by="canned")
        else:
            res = self.brain.ask(prompt, on_text, session_id=session, confirmed=confirmed)
        raw = res.answer
        if not res.is_error and not hit:
            self.last_qa = (question, raw)
        res.answer = guard.to_japanese(scenes.strip_directives(raw)) or raw
        if budget.overflow and not self.cut.is_set() and not res.is_error:
            if self.deck and self.pos < len(self.deck) - 1:
                self.speaker.say(MORE_SCENES)
            else:
                self.rest = budget.rest
                self.speaker.say(MORE_TEXT)
        if res.session_id:
            self.session_id = res.session_id
        self.last = self.clock()
        if res.is_error and not res.spoken:
            on_text(res.answer)
        self.awaiting_confirm = (not res.is_error) and is_confirm(res.answer)
        self.log(self.cfg, question, res.answer,
                 extra={"raw": raw if raw.strip() != res.answer else "", "trimmed": budget.overflow,
                        "brain": getattr(res, "by", ""), "rewrite": getattr(res, "rewrite", None),
                        "search": getattr(res, "search", None), "usage": getattr(res, "usage", None)})
        self.speaker.wait(timeout=120)
        extra = {}
        if self.awaiting_confirm:
            extra["scene"] = {"kind": "confirm", "data": {"text": res.answer}, "index": 0, "total": 1}
            self._screen("confirm")
        self.hub.set(state="confirm" if self.awaiting_confirm else "idle", answer=res.answer, text="", **extra)
        if not res.is_error and not self.cut.is_set() and (asks_more(res.answer) or budget.overflow):
            self._follow_up()
        return res.answer

    def _follow_up(self) -> None:
        """「続きを話しましょうか？」のあと、ボタンなしで少しのあいだ聞く（話さなければ、そのまま待つ）"""
        if self.follow is None:
            return
        gen = self.gen

        def run() -> None:
            while self.busy.is_set() and gen == self.gen:  # いまの答えの処理が終わるまで待つ
                time.sleep(0.1)
            if gen != self.gen or self.cut.is_set():
                return
            self.hub.set(state="listening", text="聞いています…")
            try:
                frames = self.follow(lambda: self.gen != gen or self.busy.is_set())
            except Exception as e:
                print(f"続きの聞き取りのエラー：{e}")
                frames = b""
            if frames and gen == self.gen:
                self.submit_audio(frames)
            elif gen == self.gen and self.hub.state.get("state") == "listening":
                self.hub.set(state="idle", text="")

        threading.Thread(target=run, daemon=True).start()

    # --- 画面を見せながら話す ---
    def _prepare(self, sc: scenes.Scene) -> None:
        """場面の絵を作る。ウェブの写真は時間がかかるので、あとから足す"""
        if not sc.kind:
            return
        sc.visual = visuals.build(sc.kind, sc.params, self.cfg)
        if sc.visual["kind"] == "web" and sc.params.get("url"):
            sc.visual["data"]["shot_pending"] = True

            def take(sc=sc):
                name = self.shot(sc.params["url"], sc.params.get("quote", ""))
                sc.visual["data"]["shot_pending"] = False
                if name:
                    sc.visual["data"]["shot"] = f"/shot/{name}"
                    if 0 <= self.pos < len(self.deck) and self.deck[self.pos] is sc:
                        self._show(self.pos)
            threading.Thread(target=take, daemon=True).start()
        elif sc.visual["kind"] == "sources":
            # 調べたページの一覧：上から SOURCE_SHOTS 件の写真を、裏で1件ずつ撮って足していく
            items = [it for it in (sc.visual["data"].get("items") or [])[:SOURCE_SHOTS] if it.get("url")]
            for it in items:
                it["shot_pending"] = True

            def take_all(sc=sc, items=items):
                for it in items:
                    if self.cut.is_set():
                        it["shot_pending"] = False
                        continue
                    name = self.shot(it["url"])
                    it["shot_pending"] = False
                    if name:
                        it["shot"] = f"/shot/{name}"
                    if 0 <= self.pos < len(self.deck) and self.deck[self.pos] is sc:
                        self._show(self.pos, state=self.hub.state.get("state") or "speaking")
            if items:
                threading.Thread(target=take_all, daemon=True).start()

    def _show(self, i: int, state: str = "speaking") -> None:
        sc = self.deck[i]
        last_visual = next((self.deck[j].visual for j in range(i, -1, -1) if self.deck[j].visual), None)
        scene = dict(last_visual, index=i, total=len(self.deck)) if last_visual else None
        self._screen(scene["kind"] if scene else None)
        self.hub.set(state=state, text=sc.speech, scene=scene)

    def _screen(self, kind: str | None) -> None:
        """画面の種類が変わったときだけ、開く・閉じる音を鳴らす"""
        if kind and kind != self.on_screen:
            self.sfx.play("open")
        elif not kind and self.on_screen:
            self.sfx.play("close")
        self.on_screen = kind

    def _play(self, start: int, stop: int | None = None, budget: guard.Budget | None = None) -> None:
        """start の場面から順に、話し終えるのを待ちながら進める。budget があれば、その文の数まで"""
        end = len(self.deck) if stop is None else min(stop, len(self.deck))
        for i in range(start, end):
            if self.cut.is_set():
                return
            text = self.deck[i].speech
            if budget is not None and text:
                text = budget.take(text, opener_ok=False)  # 場面の文は全部数える
                if not text:
                    return  # 決まった文の数を話し終えた。残りの場面は「次」で
            self.pos = i
            self._show(i)
            if text:
                print(f"🔊 {text}")
                self.speaker.say(text)
                self.speaker.wait(timeout=120)

    def _navigate(self, question: str) -> bool:
        q = question.strip()
        if self.rest and NEXT_RE.match(q) and not (self.deck and self.pos < len(self.deck) - 1):
            budget = guard.Budget()
            said = budget.take("".join(self.rest))
            self.rest = budget.rest
            print(f"🔊 {said}")
            self.hub.set(state="speaking", text=said)
            self.speaker.say(said)
            if self.rest:
                self.speaker.say(MORE_TEXT)
            self.speaker.wait(timeout=120)
            self.hub.set(state="idle", text="")
            return True
        if not self.deck and not self.hub.state.get("scene"):
            return False
        if BIG_RE.match(q) or SMALL_RE.match(q):
            self.hub.set(zoom=bool(BIG_RE.match(q)))
            return True
        if CLOSE_RE.match(q):
            self.deck, self.pos = [], -1
            self._screen(None)
            self.hub.set(state="idle", scene=None, text="")
            return True
        if not self.deck:
            return False
        if NEXT_RE.match(q):
            target = self.pos + 1
            if target >= len(self.deck):
                self.speaker.say("これで最後です")
                self.hub.set(state="idle")
                return True
        elif BACK_RE.match(q):
            target = max(0, self.pos - 1)
        elif AGAIN_RE.match(q):
            target = 0
        else:
            return False
        self._play(target, None if AGAIN_RE.match(q) else target + 1)
        self.speaker.wait(timeout=120)
        self.hub.set(state="idle", text="")
        return True

    def _save_canned(self, question: str) -> str:
        """「今の答えを定番にして」：直前の答えを、同じ言い方で聞かれたら返す定番として保存する"""
        if not self.last_qa:
            return self._local(question, "定番にできる答えが、まだありません")
        q, raw = self.last_qa
        path = canned.save(q, raw, canned.saved_dir(self.cfg))
        print(f"（定番に保存しました：{path}）")
        return self._local(question, "今の答えを定番にしました。次からは同じ答えをすぐ返します")

    def _local(self, question: str, answer: str) -> str:
        """Claude を呼ばずに返す返事"""
        print(f"🔊 {answer}")
        self.speaker.say(answer)
        self.log(self.cfg, question, answer)
        self.hub.set(state="idle", question=question, answer=answer, text="")
        return answer
