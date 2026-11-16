import { test, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import worker from "../src/index.js";
import { normalizeReview, applyReview } from "../src/closet.js";

const env = { GITHUB_TOKEN: "gh-test", ACCESS_KEY: "himitsu", REPO_OWNER: "owner", REPO_NAME: "Cmd", BRANCH: "main" };
const ORIGIN = "https://keigo29.github.io";

let file; // { text, sha } または null
let puts;
let conflicts;
let realFetch;

const b64 = (s) => Buffer.from(s, "utf8").toString("base64");
const unb64 = (s) => Buffer.from(s, "base64").toString("utf8");

beforeEach(() => {
  file = null;
  puts = [];
  conflicts = 0;
  realFetch = globalThis.fetch;
  globalThis.fetch = async (url, init = {}) => {
    const u = String(url);
    if (!u.includes("/contents/Data/closet/reviews.json")) return new Response("unexpected", { status: 500 });
    if ((init.method || "GET") === "GET") {
      if (!file) return new Response("", { status: 404 });
      return new Response(JSON.stringify({ content: b64(file.text), sha: file.sha }), { status: 200 });
    }
    const body = JSON.parse(init.body);
    puts.push(body);
    if (conflicts > 0) {
      conflicts--;
      return new Response("", { status: 409 });
    }
    file = { text: unb64(body.content), sha: `sha${puts.length}` };
    return new Response("{}", { status: 201 });
  };
});
afterEach(() => {
  globalThis.fetch = realFetch;
});

function post(body, { key = "himitsu", origin = ORIGIN } = {}) {
  return worker.fetch(
    new Request("https://w.example/closet/review", {
      method: "POST",
      headers: { Authorization: `Bearer ${key}`, "Content-Type": "application/json", Origin: origin },
      body: JSON.stringify(body),
    }),
    env,
  );
}

const review = { id: "today-2026-11-16", date: "2026-11-16", source: "today", items: ["outer-002", "bottom-001"], vote: 1 };

test("OPTIONS に CORS で答える", async () => {
  const res = await worker.fetch(
    new Request("https://w.example/closet/review", { method: "OPTIONS", headers: { Origin: ORIGIN } }),
    env,
  );
  assert.equal(res.status, 204);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
});

test("知らないサイトには Allow-Origin を付けない", async () => {
  const res = await worker.fetch(
    new Request("https://w.example/closet/review", { method: "OPTIONS", headers: { Origin: "https://evil.example" } }),
    env,
  );
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), null);
});

test("合言葉が違えば 401 で、書き込まない", async () => {
  const res = await post(review, { key: "wrong" });
  assert.equal(res.status, 401);
  assert.equal(puts.length, 0);
});

test("形がおかしければ 400", async () => {
  const res = await post({ id: "x", date: "きょう", items: [] });
  assert.equal(res.status, 400);
  assert.equal(puts.length, 0);
});

test("ファイルがなければ作り、あれば同じ id を上書きする", async () => {
  let res = await post(review);
  assert.equal(res.status, 200);
  assert.equal(puts[0].sha, undefined);
  assert.equal(puts[0].branch, "main");
  res = await post({ ...review, vote: -1 });
  assert.equal(res.status, 200);
  assert.equal(puts[1].sha, "sha1");
  const data = JSON.parse(file.text);
  assert.equal(data.reviews.length, 1);
  assert.equal(data.reviews[0].vote, -1);
  assert.equal(res.headers.get("Access-Control-Allow-Origin"), ORIGIN);
});

test("delete で消す", async () => {
  await post(review);
  await post({ id: review.id, delete: true });
  assert.deepEqual(JSON.parse(file.text).reviews, []);
});

test("ぶつかったら読み直してやり直す", async () => {
  conflicts = 1;
  const res = await post(review);
  assert.equal(res.status, 200);
  assert.equal(puts.length, 2);
});

test("normalizeReview と applyReview", () => {
  assert.equal(normalizeReview({ ...review, items: ["../x"] }, "t"), null);
  const r = normalizeReview({ ...review, vote: 5 }, "t");
  assert.equal(r.vote, 0);
  assert.equal(r.at, "t");
  assert.deepEqual(applyReview([{ id: "a" }, { id: "b" }], { id: "a", vote: 1 }), [{ id: "b" }, { id: "a", vote: 1 }]);
});
