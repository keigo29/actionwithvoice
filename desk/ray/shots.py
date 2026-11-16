"""ウェブページの写真を撮る（playwright が入っているときだけ。無ければ引用カードだけ出す）"""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

SHOT_DIR = Path(tempfile.gettempdir()) / "ray-shots"


def available() -> bool:
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return False
    return True


def name_for(url: str) -> str:
    return hashlib.sha1(url.encode()).hexdigest()[:16] + ".png"


def capture(url: str, quote: str = "") -> str | None:
    """写真を撮って、ファイル名を返す。撮れなければ None"""
    if not url.startswith(("http://", "https://")) or not available():
        return None
    from playwright.sync_api import sync_playwright

    SHOT_DIR.mkdir(parents=True, exist_ok=True)
    name = name_for(url + quote)
    out = SHOT_DIR / name
    if out.exists():
        return name
    try:
        with sync_playwright() as p:
            b = p.chromium.launch()
            page = b.new_page(viewport={"width": 1100, "height": 760})
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            page.wait_for_timeout(800)
            if quote:
                # 引用した文のところまで動かして、印をつける
                page.evaluate(
                    """q => {
                      const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
                      let n; while ((n = w.nextNode())) {
                        const i = n.nodeValue.indexOf(q.slice(0, 24));
                        if (i >= 0) { const el = n.parentElement;
                          el.style.background = '#fff3a0'; el.style.outline = '3px solid #ffae00';
                          el.scrollIntoView({block: 'center'}); break; }
                      }
                    }""",
                    quote,
                )
            page.screenshot(path=str(out))
            b.close()
        return name
    except Exception as e:  # noqa: BLE001
        print(f"ページの写真を撮れませんでした：{e}")
        return None
