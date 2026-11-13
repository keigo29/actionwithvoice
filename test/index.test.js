import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import worker, { parseTodo, jstParts, buildMessages } from "../src/index.js";
import { isNote } from "../src/github.js";

const env = {
  GITHUB_TOKEN: "gh-test",
  OPENAI_API_KEY: "sk-test",
  ACCESS_KEY: "himitsu",
  REPO_OWNER: "owner",
  REPO_NAME: "Cmd",
  BRANCH: "main",
};

let calls;
let realFetch;
let notes;

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  calls = [];
  notes = {
    "Inbox.md": "# Inbox\n- [ ] 牛乳を買う",
    "Notes/筋トレの記録.md": "ベンチプレス 60kg",
    "Notes/家計.md": "貯金は毎月3万円",
  };
  notes[jstParts().dailyPath] = "## 予定\n- 19:00 飲み会";
  realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const body = init.body ? JSON.parse(init.body) : null;
    calls.push({ url: String(url), method: init.method || "GET", headers: init.headers, body });
    if (String(url).includes("/git/trees/main")) {
      return json({
        tree: [
          ...Object.keys(notes).map((path) => ({ path, type: "blob" })),
          { path: "Data/voice/done/x.json", type: "blob" },
          { path: "Templates/デイリー.md", type: "blob" },
        ],
      });
    }
    if (String(url).endsWith("/graphql")) {
      const repository = {};
      for (const [k, v] of Object.entries(body.variables)) {
        if (!k.startsWith("p")) continue;
        const path = v.replace(/^main:/, "");
        repository[`f${k.slice(1)}`] = path in notes ? { text: notes[path] } : null;
      }
      return json({ data: { repository } });
    }
    if (String(url).includes("api.openai.com")) {
      return json({ choices: [{ message: { content: " ベンチプレスは60キロでした。 " } }] });
    }
    if (String(url).includes("/contents/")) return json({ content: {} }, 201);
    return new Response("unexpected", { status: 500 });
  };
});

afterEach(() => {
  globalThis.fetch = realFetch;
});

function ask(body, { key = "himitsu", type = "application/json", query = "" } = {}) {
  return new Request(`https://example.com/ask${query}`, {
    method: "POST",
    headers: { Authorization: `Bearer ${key}`, "Content-Type": type },
    body: type === "application/json" ? JSON.stringify(body) : body,
  });
}

const ctx = { pending: [], waitUntil(p) { this.pending.push(p); } };

test("parseTodo は「やること、〜」を取り出す", () => {
  assert.equal(parseTodo("やること、牛乳を買う。"), "牛乳を買う");
  assert.equal(parseTodo("タスク 歯医者を予約"), "歯医者を予約");
  assert.equal(parseTodo("TODO: 本を返す"), "本を返す");
  assert.equal(parseTodo("今日やることは何？"), null);
  assert.equal(parseTodo("やること"), null);
});

test("jstParts は日本時間で日付を作る", () => {
  const p = jstParts(new Date("2026-11-13T16:30:05Z"));
  assert.equal(p.iso, "2026-11-14T01:30:05+09:00");
  assert.equal(p.fileStamp, "2026-11-14-013005");
  assert.equal(p.dailyPath, "2026/11/14/デイリー.md");
  assert.equal(p.label, "2026-11-14（土） 01:30");
});

test("isNote はデータやテンプレートを読まない", () => {
  assert.ok(isNote("Notes/a.md"));
  assert.ok(!isNote("Data/voice/a.md"));
  assert.ok(!isNote("Templates/デイリー.md"));
  assert.ok(!isNote("Notes/a.json"));
});

test("buildMessages は現在の日時とノートを入れる", () => {
  const m = buildMessages("質問", [{ path: "Inbox.md", text: "本文" }], jstParts(new Date("2026-11-13T00:00:00Z")));
  assert.match(m[0].content, /現在の日時：2026-11-13（金） 09:00/);
  assert.match(m[1].content, /## ファイル：Inbox.md\n本文/);
  assert.match(m[1].content, /# 質問\n質問$/);
});

test("合言葉が違うと 401", async () => {
  const res = await worker.fetch(ask({ question: "a" }, { key: "chigau" }), env, ctx);
  assert.equal(res.status, 401);
  assert.equal(calls.length, 0);
});

test("ACCESS_KEY が未設定なら誰も使えない", async () => {
  const res = await worker.fetch(ask({ question: "a" }, { key: "" }), { ...env, ACCESS_KEY: "" }, ctx);
  assert.equal(res.status, 401);
});

test("空の質問は 400", async () => {
  const res = await worker.fetch(ask({ question: "  " }), env, ctx);
  assert.equal(res.status, 400);
});

test("質問に答えて、Q&A を Data/voice に置く", async () => {
  ctx.pending = [];
  const res = await worker.fetch(ask({ question: "ベンチプレスは何キロ？" }), env, ctx);
  assert.equal(res.status, 200);
  assert.equal(await res.text(), "ベンチプレスは60キロでした。");
  await Promise.all(ctx.pending);

  const ai = calls.find((c) => c.url.includes("openai"));
  assert.equal(ai.body.model, "gpt-4o-mini");
  const userMsg = ai.body.messages[1].content;
  assert.match(userMsg, /デイリー\.md/);
  assert.match(userMsg, /Inbox\.md/);
  assert.match(userMsg, /筋トレの記録/);
  assert.doesNotMatch(userMsg, /貯金/); // 関係ないノートは渡さない

  const gql = calls.find((c) => c.url.endsWith("/graphql"));
  assert.ok(!Object.values(gql.body.variables).some((v) => String(v).includes("Templates")));

  const put = calls.find((c) => c.method === "PUT");
  assert.match(put.url, /\/repos\/owner\/Cmd\/contents\/Data\/voice\/\d{4}-\d{2}-\d{2}-\d{6}\.json$/);
  const saved = JSON.parse(Buffer.from(put.body.content, "base64").toString("utf8"));
  assert.equal(saved.question, "ベンチプレスは何キロ？");
  assert.equal(saved.answer, "ベンチプレスは60キロでした。");
  assert.match(saved.datetime, /\+09:00$/);
});

test("format=json なら回答と読んだノートを返す", async () => {
  const res = await worker.fetch(ask({ question: "ベンチプレス" }, { query: "?format=json" }), env, ctx);
  const data = await res.json();
  assert.equal(data.answer, "ベンチプレスは60キロでした。");
  assert.ok(data.sources.includes("Notes/筋トレの記録.md"));
});

test("テキストのままでも質問を受け取る", async () => {
  const res = await worker.fetch(ask("ベンチプレス", { type: "text/plain" }), env, ctx);
  assert.equal(res.status, 200);
});

test("「やること、〜」は AI を呼ばずに todo として置く", async () => {
  const res = await worker.fetch(ask({ question: "やること、牛乳を買う" }), env, ctx);
  assert.equal(await res.text(), "Inbox に「牛乳を買う」を追加しました");
  assert.ok(!calls.some((c) => c.url.includes("openai")));
  const put = calls.find((c) => c.method === "PUT");
  const saved = JSON.parse(Buffer.from(put.body.content, "base64").toString("utf8"));
  assert.equal(saved.todo, "牛乳を買う");
});

test("同じ名前のファイルがあれば -2 を付けて置く", async () => {
  const base = globalThis.fetch;
  let first = true;
  globalThis.fetch = async (url, init) => {
    if (String(url).includes("/contents/") && first) {
      first = false;
      calls.push({ url: String(url), method: "PUT" });
      return json({ message: "exists" }, 422);
    }
    return base(url, init);
  };
  await worker.fetch(ask({ question: "やること、本を返す" }), env, ctx);
  const puts = calls.filter((c) => c.method === "PUT");
  assert.equal(puts.length, 2);
  assert.match(puts[1].url, /-2\.json$/);
});

test("AI が失敗したら 502 で理由を返す", async () => {
  const base = globalThis.fetch;
  globalThis.fetch = async (url, init) =>
    String(url).includes("openai") ? new Response("x", { status: 429 }) : base(url, init);
  const res = await worker.fetch(ask({ question: "ベンチ" }), env, ctx);
  assert.equal(res.status, 502);
  assert.match(await res.text(), /AI の呼び出しに失敗しました（429）/);
});

test("GET / は ok、ほかの場所は 404", async () => {
  assert.equal(await (await worker.fetch(new Request("https://example.com/"), env, ctx)).text(), "ok");
  assert.equal((await worker.fetch(new Request("https://example.com/x"), env, ctx)).status, 404);
  assert.equal((await worker.fetch(new Request("https://example.com/ask"), env, ctx)).status, 405);
});
