# ローズ（デスク常駐型の音声アシスタント・無料版）

机の前で、Mac のマイクとスピーカーだけで話しかけられるアシスタントです。頭脳は Claude Code（`claude -p`、Pro の枠）で、Cmd のローカルクローンを作業場所にして動きます。**追加の料金はかかりません**（API キーは使いません）。設計メモは Cmd の `Notes/音声連携.md` の「デスク常駐型アシスタント（ROSE 風）」にあります。

```
右 Option を押している間に話す（またはウェイクワード）
→ 録音 → Whisper（Mac の中で文字にする）
→ claude -p（作業場所は Cmd。返事は短く・記号なし）
→ 届いた文から順に say で読み上げ ＋ 光る円の画面に表示
→ Q&A を Cmd の Data/voice/ に置いて push（Actions がその日のデイリーに追記）
```

## できること（例）

- 「Cmd で一番大きいフォルダは？」「今日のデイリー読んで」「Inbox に牛乳を買うを追加して」
- 「git の状態教えて」「このプロジェクトのテスト回して」
- 続けて話すと前の話の続きになります（5分以内）。「新しい話」で切り替え
- 話している途中でもキーを押せば止まって、次を聞きます

## 安全のための決まり

| 区分 | 中身 |
|---|---|
| 確認なしでしてよい | 読む・調べる（Read / Grep / ls / du など）、Cmd のファイルの編集、git の status / diff / log / add / commit / pull / push（main へのふつうの push）、テストの実行 |
| 「確認です。〜よいですか？」と聞いてから | 上以外のコマンド全部（削除、ほかのリポジトリへの操作など）。「はい」で実行、「いいえ」でやめる |
| 確認されてもしない | `sudo`、強制 push、`git reset --hard` |

画面の円が赤く点滅していたら、確認の返事を待っています。

## はじめの設定（Mac・1回だけ）

1. **Claude Code**：`npm i -g @anthropic-ai/claude-code` → `claude` を1回起動して Pro のアカウントでログイン
2. **Cmd のクローン**：`git clone https://github.com/keigo29/Cmd.git ~/Cmd`（push できる状態にしておく）
3. **Python の準備**（このフォルダで）
   ```sh
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp config.example.toml config.toml   # 必要なら中身を直す
   ```
   - Apple Silicon なら mlx-whisper が入ります（最初の1回だけモデルをダウンロード）
   - Intel Mac などは whisper.cpp を使う：`brew install whisper-cpp` ＋ モデル（`ggml-medium.bin` など）を `~/models/` に置き、`config.toml` の `[stt] engine = "whisper_cpp"`
4. **読み上げの声**：システム設定 → アクセシビリティ → 読み上げコンテンツ → システムの声 → 管理 で「Kyoko（拡張）」を入れると自然になります
5. **確認**：`python -m rose --check`（全部 ✅ になれば OK）

## 動かす

```sh
cd desk && source .venv/bin/activate
python -m rose                              # 常駐。ブラウザに円の画面が開く
python -m rose --text 今日のデイリー読んで     # マイクなしで1回だけ試す
```

最初に起動したとき、macOS が次の許可を求めます。ターミナル（または使っているアプリ）に許可してください。

- **マイク**（録音のため）
- **入力監視**（キーを押している間を知るため。システム設定 → プライバシーとセキュリティ → 入力監視）

ログインしたら自動で起動させたいときは、「自動化」の「ログイン項目」にシェルスクリプトを登録するか、launchd を使います（必要になったら足す）。

## ウェイクワード（任意）

声で呼ぶには、openWakeWord のモデルが必要です。「ローズ」のような日本語の呼び名は用意されていないので、自分で作ります。

1. `pip install openwakeword`
2. openWakeWord の学習用ノートブック（Google Colab で無料で動く）で「ローズ」を学習し、`.onnx` を `~/models/rose.onnx` に置く
3. `config.toml` の `[wake]` を `enabled = true`、`model = "~/models/rose.onnx"` にする

呼ばれたら円が緑になり、黙るまで（1.2秒）聞き取ります。誤反応が多ければ `threshold` を上げてください。ウェイクワードを使っている間はマイクが開きっぱなしになりますが、音は Mac の外には出ません。

## 料金

すべて無料です。Claude Code は Pro の枠で動きます（ふだんの Claude Code と同じ上限を分け合います）。`ANTHROPIC_API_KEY` が設定されていても、ローズは子プロセスに渡さないので API の課金にはなりません。

## テスト

```sh
python3 -m unittest discover -s tests -t .   # マイク・スピーカーなしで動く
```

## ファイル

| ファイル | 中身 |
|---|---|
| `rose/__main__.py` | 起動・キー入力・`--check` / `--text` |
| `rose/app.py` | 会話の流れ（続きの会話・確認・リセット・ログ） |
| `rose/brain.py` | `claude -p` の呼び出しと、許可するツール・声用の指示 |
| `rose/stt.py` | 音声入力（mlx-whisper / whisper.cpp） |
| `rose/speech.py` | 読み上げ（say）と、記号を消す整形 |
| `rose/audio.py` | 録音・黙ったら止める判定 |
| `rose/wake.py` | ウェイクワード |
| `rose/ui.py`・`rose/ui/index.html` | 光る円の画面（http://localhost:8765） |
| `rose/cmdlog.py` | Q&A を Cmd の Data/voice/ に置いて push |
