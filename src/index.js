// 音声アシスタントのバックエンド（第2段階）
// iPhone のショートカット「聞く」から質問を受け取り、Cmd 全体から関係するノートを選んで AI に答えさせる。
// Q&A は Cmd の Data/voice/ に置く（Cmd の Actions がその日のデイリーに追記する）。

import { listNotes, readFiles, createFile } from "./github.js";
import { selectDocs } from "./search.js";

const JST_OFFSET = 9 * 60 * 60 * 1000;

/** 日本時間の日付・時刻の部品 */
export function jstParts(now = new Date()) {
  const d = new Date(now.getTime() + JST_OFFSET);
  const pad = (n) => String(n).padStart(2, "0");
  const y = d.getUTCFullYear();
  const mo = pad(d.getUTCMonth() + 1);
  const da = pad(d.getUTCDate());
  const h = pad(d.getUTCHours());
  const mi = pad(d.getUTCMinutes());
  const s = pad(d.getUTCSeconds());
  const week = "日月火水木金土"[d.getUTCDay()];
  return {
    iso: `${y}-${mo}-${da}T${h}:${mi}:${s}+09:00`,
    fileStamp: `${y}-${mo}-${da}-${h}${mi}${s}`,
    dailyPath: `${y}/${mo}/${da}/デイリー.md`,
    label: `${y}-${mo}-${da}（${week}） ${h}:${mi}`,
  };
}

/** 「やること、牛乳を買う」のような言い方なら、やることの中身を返す */
export function parseTodo(question) {
  const m = question.match(/^\s*(?:やること|タスク|todo)\s*[、,:：。]?\s*(.+)$/is);
  if (!m) return null;
  const todo = m[1].replace(/\s+/g, " ").replace(/[。．.]$/, "").trim();
  return todo || null;
}

export function buildMessages(question, docs, now = jstParts()) {
  const system = [
    "あなたは持ち主専用の音声アシスタントです。回答は読み上げられるので、簡潔な日本語で、原則3文以内にしてください。",
    "箇条書きや Markdown 記号、URL は使わないでください。",
    "下の「Cmd のノート」は持ち主の個人ナレッジ・やること・予定です。それで答えられる質問には、それを使って答えてください。",
    "ノートに書かれていないことを、書いてあるかのように言わないでください。わからなければ、わからないと短く言ってください。",
    `現在の日時：${now.label}（日本時間）`,
  ].join("\n");
  const notes = docs.map((d) => `## ファイル：${d.path}\n${d.text}`).join("\n\n");
  const user = `# Cmd のノート\n\n${notes || "（関係しそうなノートはありませんでした）"}\n\n# 質問\n${question}`;
  return [
    { role: "system", content: system },
    { role: "user", content: user },
  ];
}

async function askAI(env, messages) {
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: { Authorization: `Bearer ${env.OPENAI_API_KEY}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: env.MODEL || "gpt-4o-mini", messages }),
  });
  if (!res.ok) throw new Error(`AI の呼び出しに失敗しました（${res.status}）`);
  const data = await res.json();
  const answer = data.choices?.[0]?.message?.content?.trim();
  if (!answer) throw new Error("AI から回答が返ってきませんでした");
  return answer;
}

async function readQuestion(request) {
  const type = request.headers.get("Content-Type") || "";
  if (type.includes("application/json")) {
    const body = await request.json().catch(() => ({}));
    return String(body.question || body.q || "").trim();
  }
  if (type.includes("form")) {
    const form = await request.formData();
    return String(form.get("question") || form.get("q") || "").trim();
  }
  return (await request.text()).trim();
}

function reply(text, status, asJson, extra = {}) {
  if (asJson) {
    return new Response(JSON.stringify({ answer: text, ...extra }), {
      status,
      headers: { "Content-Type": "application/json; charset=utf-8" },
    });
  }
  return new Response(text, { status, headers: { "Content-Type": "text/plain; charset=utf-8" } });
}

/** 合言葉が合っているか（長さで差が出ないように比べる） */
function checkKey(request, env) {
  if (!env.ACCESS_KEY) return false;
  const given = (request.headers.get("Authorization") || "").replace(/^Bearer\s+/i, "");
  const a = new TextEncoder().encode(given);
  const b = new TextEncoder().encode(env.ACCESS_KEY);
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a[i] || 0) ^ (b[i] || 0);
  return diff === 0;
}

export async function handleAsk(request, env, ctx) {
  const url = new URL(request.url);
  const asJson = url.searchParams.get("format") === "json";
  if (!checkKey(request, env)) return reply("合言葉が違います", 401, asJson);

  const question = await readQuestion(request);
  if (!question) return reply("質問が聞き取れませんでした", 400, asJson);
  const now = jstParts();

  const todo = parseTodo(question);
  if (todo) {
    const record = JSON.stringify({ datetime: now.iso, todo }, null, 2) + "\n";
    await createFile(env, `Data/voice/${now.fileStamp}.json`, record, `音声でやることを追加 ${now.fileStamp}`);
    return reply(`Inbox に「${todo}」を追加しました`, 200, asJson, { todo });
  }

  const paths = await listNotes(env);
  const docs = await readFiles(env, paths);
  const picked = selectDocs(question, docs, { always: [now.dailyPath, "Inbox.md"] });
  const answer = await askAI(env, buildMessages(question, picked, now));

  // 保存は回答を返したあとに続ける（待たせないため）
  const record = JSON.stringify({ datetime: now.iso, question, answer }, null, 2) + "\n";
  const save = createFile(env, `Data/voice/${now.fileStamp}.json`, record, `音声Q&A ${now.fileStamp}`).catch((e) =>
    console.error(e),
  );
  if (ctx?.waitUntil) ctx.waitUntil(save);
  else await save;

  return reply(answer, 200, asJson, { sources: picked.map((d) => d.path) });
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") return new Response("ok");
    if (url.pathname !== "/ask") return new Response("Not Found", { status: 404 });
    if (request.method !== "POST") return new Response("POST で送ってください", { status: 405 });
    try {
      return await handleAsk(request, env, ctx);
    } catch (e) {
      console.error(e);
      const asJson = url.searchParams.get("format") === "json";
      return reply(`すみません、うまく答えられませんでした。${e.message}`, 502, asJson);
    }
  },
};
