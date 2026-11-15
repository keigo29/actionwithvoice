// Cmd リポジトリの読み書き（GitHub API）

const API = "https://api.github.com";

function headers(env, extra = {}) {
  return {
    Authorization: `Bearer ${env.GITHUB_TOKEN}`,
    "User-Agent": "actionwithvoice",
    "X-GitHub-Api-Version": "2022-11-28",
    ...extra,
  };
}

function repo(env) {
  return {
    owner: env.REPO_OWNER || "keigo29",
    name: env.REPO_NAME || "Cmd",
    branch: env.BRANCH || "main",
  };
}

/** 読む対象のノートか（データ置き場・設定・テンプレートは読まない） */
export function isNote(path) {
  if (!path.endsWith(".md")) return false;
  return !/^(Data|Templates|scripts|\.github|\.obsidian)\//.test(path);
}

/** リポジトリの中の Markdown のパス一覧 */
export async function listNotes(env) {
  const r = repo(env);
  const res = await fetch(`${API}/repos/${r.owner}/${r.name}/git/trees/${r.branch}?recursive=1`, {
    headers: headers(env, { Accept: "application/vnd.github+json" }),
  });
  if (!res.ok) throw new Error(`ファイル一覧の取得に失敗しました（${res.status}）`);
  const data = await res.json();
  return data.tree.filter((t) => t.type === "blob" && isNote(t.path)).map((t) => t.path);
}

/** 複数のファイルの中身を、GraphQL でまとめて1回で読む（100件ずつ） */
export async function readFiles(env, paths) {
  const r = repo(env);
  const out = [];
  for (let start = 0; start < paths.length; start += 100) {
    const chunk = paths.slice(start, start + 100);
    const vars = { owner: r.owner, name: r.name };
    const decl = ["$owner: String!", "$name: String!"];
    const fields = chunk.map((p, i) => {
      vars[`p${i}`] = `${r.branch}:${p}`;
      decl.push(`$p${i}: String!`);
      return `f${i}: object(expression: $p${i}) { ... on Blob { text } }`;
    });
    const query = `query(${decl.join(", ")}) { repository(owner: $owner, name: $name) { ${fields.join(" ")} } }`;
    const res = await fetch(`${API}/graphql`, {
      method: "POST",
      headers: headers(env, { "Content-Type": "application/json" }),
      body: JSON.stringify({ query, variables: vars }),
    });
    if (!res.ok) throw new Error(`ノートの読み取りに失敗しました（${res.status}）`);
    const data = await res.json();
    if (data.errors && !data.data) throw new Error(`ノートの読み取りに失敗しました：${data.errors[0]?.message}`);
    const repoData = data.data?.repository || {};
    chunk.forEach((p, i) => {
      const text = repoData[`f${i}`]?.text;
      if (typeof text === "string") out.push({ path: p, text });
    });
  }
  return out;
}

function toBase64(text) {
  const bytes = new TextEncoder().encode(text);
  let bin = "";
  for (const b of bytes) bin += String.fromCharCode(b);
  return btoa(bin);
}

/** 新しいファイルを置く（同じ名前があれば -2, -3 … を付ける） */
export async function createFile(env, path, text, message) {
  const r = repo(env);
  for (let n = 1; n <= 5; n++) {
    const p = n === 1 ? path : path.replace(/(\.[^.]+)$/, `-${n}$1`);
    const url = `${API}/repos/${r.owner}/${r.name}/contents/${p.split("/").map(encodeURIComponent).join("/")}`;
    const res = await fetch(url, {
      method: "PUT",
      headers: headers(env, { Accept: "application/vnd.github+json", "Content-Type": "application/json" }),
      body: JSON.stringify({ message, content: toBase64(text), branch: r.branch }),
    });
    if (res.ok) return p;
    if (res.status !== 422) throw new Error(`保存に失敗しました（${res.status}）`);
  }
  throw new Error("保存に失敗しました（同じ名前のファイルが多すぎます）");
}
