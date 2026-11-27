"""ウェブページの写真を撮る（playwright が入っているときだけ。無ければ引用カードだけ出す）

ブラウザ（Chromium）は1つだけ起動して使い回す（写真のたびに起動すると、そのたびに CPU とメモリを取り、
話している最中の声作りや聞き取りが遅れる）。しばらく使わなければ閉じてメモリを返す。
playwright は、起動したスレッドからしか使えないので、写真を撮る係のスレッドを1つ持ち、頼まれた順に撮る。
"""

from __future__ import annotations

import hashlib
import queue
import tempfile
import threading
from pathlib import Path

SHOT_DIR = Path(tempfile.gettempdir()) / "ray-shots"
IDLE_SEC = 300      # これだけ使わなければ、ブラウザを閉じる
WAIT_SEC = 40       # 1枚を待つ長さ（撮る係が詰まっていても、呼んだ側は先に進む）

MARK_JS = """q => {
  const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n; while ((n = w.nextNode())) {
    const i = n.nodeValue.indexOf(q.slice(0, 24));
    if (i >= 0) { const el = n.parentElement;
      el.style.background = '#fff3a0'; el.style.outline = '3px solid #ffae00';
      el.scrollIntoView({block: 'center'}); break; }
  }
}"""


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def name_for(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ".png"


class Shooter:
    """写真を撮る係。ブラウザを1つ持ち、頼まれた順に撮る"""

    def __init__(self, launch=None, idle_sec: float = IDLE_SEC):
        self.launch = launch or self._launch  # () -> (browser, stop)。テストで差し替える
        self.idle_sec = idle_sec
        self.q: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.lock = threading.Lock()
        self.launches = 0

    @staticmethod
    def _launch():
        from playwright.sync_api import sync_playwright
        p = sync_playwright().start()
        b = p.chromium.launch()

        def stop():
            try:
                b.close()
            finally:
                p.stop()
        return b, stop

    def shoot(self, url: str, out: Path, quote: str = "") -> bool:
        done, box = threading.Event(), {}
        with self.lock:  # 係が「もう頼まれていない」と見て閉じるのと、ぶつからないように
            self.q.put((url, out, quote, done, box))
            if self.thread is None:
                self.thread = threading.Thread(target=self._loop, daemon=True)
                self.thread.start()
        done.wait(WAIT_SEC)
        return bool(box.get("ok"))

    def _loop(self) -> None:
        browser = stop = None
        try:
            while True:
                try:
                    url, out, quote, done, box = self.q.get(timeout=self.idle_sec)
                except queue.Empty:
                    with self.lock:
                        if self.q.empty():
                            self.thread = None
                            return  # しばらく使っていない：閉じる（次に頼まれたら起動し直す）
                    continue
                try:
                    if browser is None:
                        browser, stop = self.launch()
                        self.launches += 1
                    page = browser.new_page(viewport={"width": 1100, "height": 760})
                    try:
                        page.goto(url, timeout=15000, wait_until="domcontentloaded")
                        page.wait_for_timeout(800)
                        if quote:
                            page.evaluate(MARK_JS, quote)  # 引用した文のところまで動かして、印をつける
                        page.screenshot(path=str(out))
                        box["ok"] = True
                    finally:
                        page.close()
                except Exception as e:  # noqa: BLE001
                    print(f"ページの写真を撮れませんでした：{e}")
                    if browser is not None and not _alive(browser):
                        browser = None  # ブラウザが落ちていたら、次は起動し直す
                finally:
                    done.set()
        finally:
            with self.lock:
                if self.thread is threading.current_thread():
                    self.thread = None  # 思わぬ失敗で止まったときも、次に頼まれたら起動し直す
            if stop:
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    pass


def _alive(browser) -> bool:
    try:
        return browser.is_connected()
    except Exception:  # noqa: BLE001
        return False


SHOOTER = Shooter()


def capture(url: str, quote: str = "") -> str | None:
    """写真を撮って、ファイル名を返す。撮れなければ None"""
    if not url.startswith(("http://", "https://")) or not available():
        return None
    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    name = name_for(url + quote)
    out = SHOT_DIR / name
    if out.exists():
        return name
    return name if SHOOTER.shoot(url, out, quote) else None
