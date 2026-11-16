// クローゼットアプリ（keigo29/Closet）の 👍／👎 を Cmd の Data/closet/reviews.json に書き戻す

import { readText, writeText } from "./github.js";

export const REVIEWS_PATH = "Data/closet/reviews.json";
const ALLOWED_ORIGINS = ["https://keigo29.github.io"];

export function corsHeaders(request) {
  const origin = request.headers.get("Origin") || "";
  const h = {
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Authorization, Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
  if (ALLOWED_ORIGINS.includes(origin) || /^http:\/\/localhost(:\d+)?$/.test(origin)) {
    h["Access-Control-Allow-Origin"] = origin;
  }
  return h;
}

/** 送られてきたレビューを確かめて、保存する形に整える。おかしければ null */
export function normalizeReview(body, nowIso) {
  if (!body || typeof body !== "object") return null;
  const id = String(body.id || "").trim();
  if (!/^[\w-]{1,80}$/.test(id)) return null;
  if (body.delete === true) return { id, delete: true };
  const date = String(body.date || "");
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null;
  const source = body.source === "today" ? "today" : "self";
  if (!Array.isArray(body.items) || body.items.length === 0 || body.items.length > 8) return null;
  const items = body.items.map((x) => String(x));
  if (!items.every((x) => /^[\w-]{1,40}$/.test(x))) return null;
  const vote = [1, -1, 0].includes(body.vote) ? body.vote : 0;
  return { id, date, source, items, vote, at: nowIso };
}

/** レビューの一覧に足す（同じ id は上書き、delete なら消す） */
export function applyReview(list, review) {
  const rest = list.filter((r) => r.id !== review.id);
  if (review.delete) return rest;
  return [...rest, review];
}

export async function saveReview(env, review) {
  for (let attempt = 0; attempt < 3; attempt++) {
    const current = await readText(env, REVIEWS_PATH);
    let data = { reviews: [] };
    if (current) {
      try {
        data = JSON.parse(current.text);
      } catch {
        throw new Error("reviews.json の形が壊れています");
      }
      if (!Array.isArray(data.reviews)) data.reviews = [];
    }
    data.reviews = applyReview(data.reviews, review);
    const text = JSON.stringify(data, null, 2) + "\n";
    const label = review.delete ? "消す" : review.vote === 1 ? "👍" : review.vote === -1 ? "👎" : "追加";
    const ok = await writeText(env, REVIEWS_PATH, text, `クローゼット：レビュー${label} ${review.id}`, current?.sha);
    if (ok) return data.reviews.length;
  }
  throw new Error("ほかの書き込みとぶつかりました。もう一度送ってください");
}
