// 質問に関係しそうなノートを選ぶ（日本語は単語の区切りが無いので、2文字ずつの組で比べる）

/** 比べやすい形にそろえる（全角半角・大小をそろえ、記号と空白を消す） */
export function normalize(text) {
  return text
    .normalize("NFKC")
    .toLowerCase()
    .replace(/[\s\p{P}\p{S}]+/gu, "");
}

/** 2文字ずつの組（例：「筋トレ」→「筋ト」「トレ」） */
export function bigrams(text) {
  const s = [...normalize(text)];
  const out = [];
  for (let i = 0; i + 1 < s.length; i++) out.push(s[i] + s[i + 1]);
  return out;
}

function countIn(haystack, needle) {
  let n = 0;
  let i = haystack.indexOf(needle);
  while (i !== -1) {
    n++;
    i = haystack.indexOf(needle, i + needle.length);
  }
  return n;
}

/**
 * 各ノートに点数をつけて、高い順に返す
 * @param {string} question
 * @param {{path: string, text: string}[]} docs
 * @returns {{path: string, text: string, score: number}[]}
 */
export function rank(question, docs) {
  const qGrams = [...new Set(bigrams(question))];
  if (qGrams.length === 0 || docs.length === 0) return docs.map((d) => ({ ...d, score: 0 }));
  const prepared = docs.map((d) => ({ doc: d, body: normalize(d.text), path: normalize(d.path) }));
  // どのノートにもある組（「です」など）は軽く、珍しい組は重く数える
  const idf = new Map();
  for (const g of qGrams) {
    const df = prepared.filter((p) => p.body.includes(g) || p.path.includes(g)).length;
    idf.set(g, Math.log(1 + docs.length / (1 + df)));
  }
  return prepared
    .map(({ doc, body, path }) => {
      let score = 0;
      for (const g of qGrams) {
        const tf = countIn(body, g);
        score += idf.get(g) * (tf / (tf + 1.2));
        if (path.includes(g)) score += idf.get(g) * 2; // ファイル名に入っていたら強め
      }
      return { ...doc, score };
    })
    .sort((a, b) => b.score - a.score);
}

/**
 * AI に渡すノートを選ぶ。必ず入れるもの → 点数の高い順に、文字数の上限まで
 * @param {string} question
 * @param {{path: string, text: string}[]} docs
 * @param {{always?: string[], budget?: number, perDoc?: number, maxDocs?: number}} opts
 */
export function selectDocs(question, docs, opts = {}) {
  const { always = [], budget = 30000, perDoc = 8000, maxDocs = 8, minRatio = 0.3 } = opts;
  const picked = [];
  let used = 0;
  const take = (d) => {
    const text = d.text.length > perDoc ? d.text.slice(0, perDoc) + "\n…（以下略）" : d.text;
    if (used + text.length > budget && picked.length > 0) return false;
    picked.push({ path: d.path, text });
    used += text.length;
    return true;
  };
  for (const p of always) {
    const d = docs.find((x) => x.path === p);
    if (d) take(d);
  }
  const ranked = rank(question, docs);
  const floor = (ranked[0]?.score || 0) * minRatio; // 1位よりずっと低いものは入れない
  for (const d of ranked) {
    if (picked.length >= maxDocs) break;
    if (d.score <= 0 || d.score < floor) break;
    if (picked.some((x) => x.path === d.path)) continue;
    if (!take(d)) break;
  }
  return picked;
}
