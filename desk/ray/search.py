"""検索の受け皿：検索 API で取りに行き → Gemini 無料枠で要約 → だめならローカル 14B で要約

Cmd の Notes/音声連携.md「検索の受け皿（2026-11-19）」の方針：
- Gemini 無料枠の検索（google_search）は上限が早く枯れるので、ふだんの検索は検索 API（Tavily）で結果を取る
- 要約は Gemini 無料枠（Flash-Lite）。枠が枯れた・失敗したらローカルの 14B。それも無理なら Claude
- 深い検索（いくつもの情報源を比べる）だけは、これまでどおり Gemini の検索か Claude へ（Gemini の検索枠は温存）
- 外へ出るのは検索の言葉と公開情報だけ。予定や家計などの持ち主のデータは混ぜない
  （ここに来るのは、振り分けで持ち主のデータ・作業の言葉が無いと分かった質問だけ。言葉も公開の検索結果も外に出してよい側）

検索 API は SEARCHERS で差し替えられる（Exa などを足すときは、同じ形の関数を1つ足す）。
API キーは config.toml の [search] api_key か環境変数 TAVILY_API_KEY。コードにもログにも Cmd にも書かない。
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.parse import urlparse

JST = timezone(timedelta(hours=9))
WEEKDAYS = "月火水木金土日"

SUMMARY_PROMPT = """あなたは、机の前にいる持ち主の音声アシスタント「レイ」です。返事はそのまま日本語で読み上げられます。
今は{now}です。
下の「検索結果」だけを元に、持ち主の質問に答えてください。
- 店や場所を聞かれたら、検索結果にある実在の名前を、結果に書いてあるとおりに挙げる。結果に無い名前や情報は作らない
- 検索結果に答えが無ければ「調べた範囲では分かりませんでした」とだけ言う
- 出典のサイト名は言ってよい。URL は書かない
- 持ち主の予定やファイルなどのデータが要る、作業が要るときは、答えずに「@クロード」とだけ書く
{rules}

検索結果：
{results}"""

# 検索の言葉から落とすもの（頼みの言い回し）
_ASK_WORDS = re.compile(
    r"(?:を|について|のこと)?(?:ちょっと)?(?:調べて|検索して|ググって|教えて|探して|見つけて)(?:くれる|ください|ほしい|欲しい|みて|もらえる)?"
    r"(?:？|\?|。|か|な|よ)*|(?:って|は)?(?:何|なに)(?:か|が)?(?:ある|あります)?[？?]|ってある[？?]|[？?。]"
)
_NEAR = re.compile(r"(?<![駅町区市村])(?:この辺|このへん|この近く|近所|近く|周辺|周り|まわり)(?:の|に|で)?")
_POINTING = re.compile(r"^(?:その|そこの|あの|この|そこ|それ|あれ)|(?:その|そこの|あの)")


def build_query(text: str, near: str = "", previous: str = "") -> str:
    """検索の言葉を作る。「近く」は場所の名前に置き換え、「その店の」などは前の検索の言葉を足す"""
    from .rewrite import tidy  # 言いよどみを落とす（rewrite は local に読み込まれるので遅れて読む）

    q = tidy(text)
    q = _ASK_WORDS.sub(" ", q)
    if near and _NEAR.search(q):
        q = _NEAR.sub(f"{near} ", q)
    q = re.sub(r"[、,\s]+", " ", q).strip()
    if previous and (_POINTING.search(q) or len(q) <= 6):
        q = f"{previous} {_POINTING.sub('', q).strip()}".strip()
    return q or text.strip()


# ---------------------------------------------------------------- 検索 API（差し替えられる部分）

class SearchError(Exception):
    def __init__(self, message: str, exhausted: bool = False):
        super().__init__(message)
        self.exhausted = exhausted  # 無料枠を使い切った（この月はもう使わない）


def tavily_search(query: str, c: dict, key: str, urlopen=urllib.request.urlopen) -> list[dict]:
    """Tavily の基本検索（1クレジット）。結果は [{"title", "url", "content", "site"}]"""
    body = {"query": query, "search_depth": c.get("depth", "basic"), "max_results": c["max_results"],
            "include_answer": False, "topic": "news" if re.search(r"ニュース|速報", query) else "general"}
    if c.get("country"):
        body["country"] = c["country"]

    def post(b: dict):
        req = urllib.request.Request("https://api.tavily.com/search", json.dumps(b).encode(),
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urlopen(req, timeout=c["timeout"]) as r:
            return json.loads(r.read())

    try:
        try:
            data = post(body)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "country" in body:  # 国の指定が受け付けられないときは、外してもう一度
                body.pop("country")
                data = post(body)
            else:
                raise
    except urllib.error.HTTPError as e:
        # 429 は混み合い、432・433 は枠の上限（Tavily の決まり）
        raise SearchError(f"Tavily がエラーを返しました（{e.code}）", exhausted=e.code in (432, 433)) from e
    except (OSError, ValueError) as e:
        raise SearchError(f"Tavily につながりません：{e}") from e
    out = []
    for r in data.get("results", []):
        if r.get("url") and (r.get("content") or r.get("title")):
            out.append({"title": (r.get("title") or "").strip(), "url": r["url"],
                        "content": (r.get("content") or "").strip(), "site": urlparse(r["url"]).netloc})
    return out


SEARCHERS: dict[str, Callable[..., list[dict]]] = {"tavily": tavily_search}
KEY_ENV = {"tavily": "TAVILY_API_KEY"}


def results_text(results: list[dict], n: int, chars: int = 500) -> str:
    return "\n".join(f"[{i}] {r['title']}（{r['site']}）\n{r['content'][:chars]}"
                     for i, r in enumerate(results[:n], 1))


def _now_text(now: datetime) -> str:
    n = now.astimezone(JST)
    return f"{n.year}年{n.month}月{n.day}日（{WEEKDAYS[n.weekday()]}）{n.hour}時{n.minute}分"


@dataclass
class Found:
    answer: str | None = None
    by: str = ""                      # gemini / local（要約した頭脳）
    handoff: bool = False             # 要約した頭脳が「@クロード」と返した（持ち主のデータ・作業が要る）
    log: dict = field(default_factory=dict)


class SearchBrain:
    """検索 API → 要約（Gemini 無料枠 → ローカル 14B）。答えられなければ answer=None（次の段に回す）"""

    def __init__(self, cfg: dict, gemini, local, urlopen=urllib.request.urlopen, env: dict | None = None,
                 now: Callable[[], datetime] = lambda: datetime.now(JST)):
        self.cfg = cfg
        self.c = cfg["search"]
        self.gemini = gemini
        self.local = local
        self.urlopen = urlopen
        self.now = now
        env = os.environ if env is None else env
        provider = self.c.get("provider", "tavily")
        self.search_fn = SEARCHERS.get(provider)
        self.key = self.c.get("api_key") or env.get(KEY_ENV.get(provider, ""), "")
        self.exhausted_month = None
        self.count_month, self.count = None, 0
        self.previous = ""  # 直前の検索の言葉（「その店の営業時間」などに足す）
        self.cut = threading.Event()

    def available(self) -> bool:
        if not (self.c["enabled"] and self.search_fn and self.key):
            return False
        month = self.now().strftime("%Y-%m")
        if month != self.count_month:
            self.count_month, self.count = month, 0
        return self.exhausted_month != month and self.count < self.c["monthly_limit"]

    def near(self) -> str:
        return self.c.get("near") or self.cfg.get("weather", {}).get("place", "")

    def ask(self, text: str, history: list[tuple[str, str]], on_text: Callable[[str], None]) -> Found:
        self.cut.clear()
        log: dict = {"route": "", "provider": self.c.get("provider", "tavily")}
        if not self.available():
            log["error"] = "search_unavailable"  # 鍵がない・止めてある・枠を使い切った
            return Found(log=log)
        query = build_query(text, self.near(), self.previous)
        log["query"] = query
        self.count += 1
        try:
            results = self.search_fn(query, self.c, self.key, urlopen=self.urlopen)
        except SearchError as e:
            print(e)
            if e.exhausted:
                self.exhausted_month = self.now().strftime("%Y-%m")
            log["error"] = str(e)
            return Found(log=log)
        except Exception as e:  # 検索の失敗で会話は止めない
            log["error"] = f"error: {type(e).__name__}"
            return Found(log=log)
        log["results"] = [{"title": r["title"], "url": r["url"]} for r in results[: self.c["summary_results"]]]
        if not results:
            log["error"] = "no_results"
            return Found(log=log)
        self.previous = query
        system = SUMMARY_PROMPT.format(now=_now_text(self.now()), rules=self._rules(),
                                       results=results_text(results, self.c["summary_results"]))
        sources = [{"title": r["title"] or r["site"], "url": r["url"], "site": r["site"]}
                   for r in results[: self.cfg["gemini"].get("max_sources", 5)]]
        # 要約：Gemini 無料枠 → ローカル 14B
        tried = []
        if not self.cut.is_set() and self.gemini.available():
            tried.append("gemini")
            answer = self.gemini.summarize(system, history, text, on_text)
            if answer:
                return self._found(answer, "gemini", log, tried, sources, on_text)
            if getattr(self.gemini, "last", "") == "handoff":
                log.update(route="claude", tried=tried, error="handoff")  # 14B を試さず、すぐ Claude へ
                return Found(log=log, handoff=True)
        if not self.cut.is_set() and self.cfg["local"]["enabled"] and self.c.get("local_model"):
            tried.append("local")
            answer = self.local.summarize(self.c["local_model"], system, text, on_text,
                                          timeout=self.c["local_timeout"], keep_alive=self.c["local_keep_alive"])
            if answer:
                return self._found(answer, "local", log, tried, sources, on_text)
        log.update(route="", tried=tried, error="summary_failed")
        return Found(log=log)

    def _rules(self) -> str:
        from .local import SPEAK_RULES
        return SPEAK_RULES

    def _found(self, answer, by, log, tried, sources, on_text) -> Found:
        on_text(_panel(sources))  # 調べたページの一覧を画面に出す（読み上げはしない）
        log.update(route=f"{log['provider']}+{by}", tried=tried, answer=answer)
        return Found(answer=answer, by=by, log=log)

    def cancel(self) -> None:
        self.cut.set()


def _panel(sources: list[dict]) -> str:
    return f"@画面 sources {json.dumps({'items': sources}, ensure_ascii=False)}"
