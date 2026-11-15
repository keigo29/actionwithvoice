import { test } from "node:test";
import assert from "node:assert/strict";
import { bigrams, normalize, rank, selectDocs } from "../src/search.js";

test("normalize は全角・記号・空白をそろえる", () => {
  assert.equal(normalize("ＡＢＣ　筋トレ！"), "abc筋トレ");
});

test("bigrams は2文字ずつに分ける", () => {
  assert.deepEqual(bigrams("筋トレ"), ["筋ト", "トレ"]);
  assert.deepEqual(bigrams("あ"), []);
});

const docs = [
  { path: "Notes/筋トレの記録.md", text: "ベンチプレス 60kg を 3セット。スクワットも。" },
  { path: "Notes/家計の配分分析.md", text: "食費と家賃の配分。貯金は毎月3万円。" },
  { path: "Inbox.md", text: "- [ ] 牛乳を買う" },
  { path: "2026/11/13/デイリー.md", text: "## メモ\n- 体重 70.9kg" },
];

test("rank は関係のあるノートを上にする", () => {
  assert.equal(rank("ベンチプレスは何キロだった？", docs)[0].path, "Notes/筋トレの記録.md");
  assert.equal(rank("貯金はいくら", docs)[0].path, "Notes/家計の配分分析.md");
});

test("rank はファイル名に入っている言葉も強く見る", () => {
  assert.equal(rank("家計どう？", docs)[0].path, "Notes/家計の配分分析.md");
});

test("selectDocs は必ず入れるものを先に入れ、関係ないものは入れない", () => {
  const picked = selectDocs("ベンチプレス", docs, { always: ["2026/11/13/デイリー.md", "Inbox.md", "無いファイル.md"] });
  assert.deepEqual(
    picked.map((d) => d.path),
    ["2026/11/13/デイリー.md", "Inbox.md", "Notes/筋トレの記録.md"],
  );
});

test("selectDocs は文字数の上限を守り、長いノートは切る", () => {
  const big = [
    { path: "a.md", text: "ベンチ".repeat(5000) },
    { path: "b.md", text: "ベンチ".repeat(5000) },
  ];
  const picked = selectDocs("ベンチ", big, { budget: 9000, perDoc: 8000 });
  assert.equal(picked.length, 1);
  assert.ok(picked[0].text.endsWith("（以下略）"));
});
