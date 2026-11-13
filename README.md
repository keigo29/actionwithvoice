# actionwithvoice

音声アシスタント「聞く」の第2段階のバックエンドです。iPhone のショートカットから質問を受け取り、**Cmd リポジトリ全体から関係するノートを選んで** AI に答えさせます。設計の全体像は Cmd の `Notes/音声連携.md` にあります。

```
ショートカット「聞く」（音声入力）
→ POST /ask（このバックエンド・Cloudflare Workers）
   1. Cmd のノート一覧を取り、中身をまとめて読む（GitHub API 2回）
   2. 今日のデイリー・Inbox ＋ 質問に近いノート（最大8件・3万文字まで）を選ぶ
   3. AI（既定は gpt-4o-mini）に渡して回答をもらう
   4. Q&A を Cmd の Data/voice/ に置く（Cmd の Actions がデイリーに追記）
→ 回答の文字だけが返る → ショートカットが読み上げる
```

- 「やること、牛乳を買う」のように **「やること」「タスク」「TODO」で始めると**、AI を呼ばずに Cmd の `Inbox.md` に `- [ ] 牛乳を買う` を足します（Cmd の Actions が反映）
- ノートの選び方は、日本語の2文字ずつの組で質問とノートを比べる簡単な方式です（`src/search.js`）。`Data/`・`Templates/`・`scripts/`・`.github/`・`.obsidian/` は読みません

## 使い方（API）

| | |
|---|---|
| URL | `https://actionwithvoice.<あなたのサブドメイン>.workers.dev/ask` |
| 方法 | POST |
| ヘッダ | `Authorization: Bearer <ACCESS_KEY>` |
| 本文 | JSON `{"question": "質問"}`（テキストそのままでも可） |
| 返事 | 回答の文字（`?format=json` を付けると `{"answer", "sources"}`） |

エラーのときも、読み上げられる日本語の文が返ります（401 合言葉違い／400 質問が空／502 GitHub や AI の失敗）。

## はじめの設定（1回だけ）

### 1. 用意するもの

| 名前 | 中身 |
|---|---|
| `CMD_TOKEN` | GitHub の Fine-grained token。Cmd だけ・Contents の Read and write（Cmd の `Notes/ヘルスケア連携.md` で作ったものでよい） |
| `OPENAI_API_KEY` | OpenAI の API キー |
| `ACCESS_KEY` | ショートカットとバックエンドの合言葉。長いでたらめな文字列（例：パスワード生成で32文字） |
| `CLOUDFLARE_API_TOKEN` | Cloudflare → My Profile → API Tokens →「Edit Cloudflare Workers」テンプレートで作る |
| `CLOUDFLARE_ACCOUNT_ID` | Cloudflare のダッシュボード右側の Account ID |

### 2. GitHub の Secrets に入れる

この actionwithvoice リポジトリの Settings → Secrets and variables → Actions →「New repository secret」で、上の5つを同じ名前で入れます。

### 3. デプロイ

main に push する（または Actions タブの「テストしてデプロイする」→ Run workflow）と、テストのあとに Cloudflare にデプロイされ、`CMD_TOKEN` などが Worker の秘密の値として渡されます。Cloudflare の設定が無いうちは、テストだけ動いてデプロイはとばされます。

デプロイ後、Actions のログか Cloudflare のダッシュボードに出る `https://actionwithvoice.〜.workers.dev` が URL です。

### 4. ショートカット「聞く」を第2段階にする

Cmd の `Notes/音声連携.md` の「第2段階：バックエンド版の聞く」を見てください。手順は「音声入力 → URL の内容を取得（POST /ask）→ 読み上げる」の3つだけになります。

## 手元で動かす

```sh
npm test                     # テスト（Node.js 22 以上、追加のインストール不要）
npx wrangler dev             # 手元で起動（.dev.vars に GITHUB_TOKEN / OPENAI_API_KEY / ACCESS_KEY を書く）
```

`.dev.vars` は `.gitignore` 済みです。API キーやトークンはファイルやコミットに含めないでください。

## 設定を変える

`wrangler.toml` の `[vars]` で、読むリポジトリ（`REPO_OWNER` / `REPO_NAME` / `BRANCH`）と AI のモデル（`MODEL`）を変えられます。

## 費用の目安

- Cloudflare Workers：無料枠（1日10万リクエスト）で足ります
  - 無料プランは1回あたりの CPU 時間が 10ms までです。今の Cmd（ノート約40件）なら手元で測って 5〜8ms ですが、ノートが大きく増えてエラー（1102）が出るようなら、`maxDocs` を減らすか有料プラン（月5ドル）にしてください
- OpenAI：ノートを最大3万文字ほど渡すので、gpt-4o-mini で1回 1円前後
