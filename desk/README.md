# Ray（レイ・デスク常駐型の音声アシスタント・無料版）

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
5. **確認**：`python -m ray --check`（全部 ✅ になれば OK）

## 動かす

```sh
cd desk && source .venv/bin/activate
python -m ray                              # 常駐。ブラウザに円の画面が開く
python -m ray --text 今日のデイリー読んで     # マイクなしで1回だけ試す
```

最初に起動したとき、macOS が次の許可を求めます。ターミナル（または使っているアプリ）に許可してください。

- **マイク**（録音のため）
- **入力監視**（キーを押している間を知るため。システム設定 → プライバシーとセキュリティ → 入力監視）

ログインしたら自動で起動させたいときは、「自動化」の「ログイン項目」にシェルスクリプトを登録するか、launchd を使います（必要になったら足す）。

## 画面を見せながら話す

予定・数字・一覧・出典があるときは、話している内容に合わせて画面に絵を出します（プレゼンのように、場面ごとに進む）。要件は Cmd の `Notes/Ray画面表示.md` にあります。

| 種類 | 画面 | データ |
|---|---|---|
| calendar | 日・週・月のカレンダー。話している予定を光らせる | デイリーの `## 予定`、`## YYYY-MM-DD` の下、`📅` の期限、`#節目` |
| note | ノートの抜き出し。根拠の行に印 | Cmd のノート |
| todos | 期限の近い順のやること（期限切れは赤）、プロジェクトの進み具合 | `- [ ]`、`Projects/` |
| habits | 毎日確認のマス目と、続いている日数 | デイリーのプロパティ、`Notes/毎日確認.md` |
| body | 体重・気分の折れ線グラフ（目標の線つき） | デイリーの `- 体重`・`- 気分`、`Data/health/` |
| kakei | カテゴリ別の支出、予算の残り | `Data/kakei.json` |
| workout | 種目ごとの重さの推移、今日のメニュー | `Data/kintore/sets.csv` |
| weather | 1時間ごとの天気 | open-meteo（30分は使い回す） |
| diff | 変えたファイルの前と後、git の状態 | Cmd の git |
| web / sources | ページの写真と引用カード、使ったページの一覧 | WebSearch・WebFetch |
| table / steps / calc | 比較の表、1つずつ進む手順、式と答え | Claude が考えた中身 |
| diagram | 仕組み・流れ・関係の図（Mermaid：フローチャート・シーケンス図・マインドマップ・タイムライン・ガントなど）。話している所のノードを光らせる | Claude が書いた Mermaid。ライブラリは最初に使うときにネットから読む |
| confirm | 「確認です。〜よいですか？」のときのカード | Ray の確認待ち |

- 「おはよう」「朝のまとめ」で、天気 → 今日の予定 → 近い期限 → 習慣 を続けて見せます
- 声での操作（Claude を呼ばずにすぐ動く）：「次」「戻って」「もう一回」「大きく」「小さく」「閉じて」。話している途中はキーを押せば止まります
- ウェブページの写真は playwright が入っているときだけ撮ります（無ければ引用カードだけ）：
  ```sh
  pip install playwright && playwright install chromium
  ```
- 天気の場所は `config.toml` の `[weather]`（`place`・`latitude`・`longitude`）で変えられます

しくみ：Claude Code の返事の中の `@画面 種類 {JSON}` の行で場面を区切り、その下の文を読み上げます（指示の行は読み上げない）。グラフやカレンダーの中身は Ray が Cmd から集めるので、Claude が大きなデータを書く必要はありません。

## ウェイクワード（任意）

声で呼ぶには、openWakeWord のモデルが必要です。「レイ」のような日本語の呼び名は用意されていないので、自分で作ります。

1. `pip install openwakeword`
2. openWakeWord の学習用ノートブック（Google Colab で無料で動く）で「レイ」を学習し、`.onnx` を `~/models/ray.onnx` に置く
3. `config.toml` の `[wake]` を `enabled = true`、`model = "~/models/ray.onnx"` にする

呼ばれたら円が緑になり、黙るまで（1.2秒）聞き取ります。誤反応が多ければ `threshold` を上げてください。ウェイクワードを使っている間はマイクが開きっぱなしになりますが、音は Mac の外には出ません。

## 料金

すべて無料です。Claude Code は Pro の枠で動きます（ふだんの Claude Code と同じ上限を分け合います）。`ANTHROPIC_API_KEY` が設定されていても、Rayは子プロセスに渡さないので API の課金にはなりません。

## テスト

```sh
python3 -m unittest discover -s tests -t .   # マイク・スピーカーなしで動く
```

## ファイル

| ファイル | 中身 |
|---|---|
| `ray/__main__.py` | 起動・キー入力・`--check` / `--text` |
| `ray/app.py` | 会話の流れ（続きの会話・確認・リセット・ログ） |
| `ray/brain.py` | `claude -p` の呼び出しと、許可するツール・声用の指示 |
| `ray/stt.py` | 音声入力（mlx-whisper / whisper.cpp） |
| `ray/speech.py` | 読み上げ（say）と、記号を消す整形 |
| `ray/audio.py` | 録音・黙ったら止める判定 |
| `ray/wake.py` | ウェイクワード |
| `ray/ui.py`・`ray/ui/index.html` | 光る円と、場面ごとの絵の画面（http://localhost:8765） |
| `ray/scenes.py` | 返事を `@画面` の行で場面に分ける |
| `ray/visuals.py` | 場面の絵のデータを Cmd から作る（カレンダー・やること・習慣など） |
| `ray/shots.py` | ウェブページの写真（playwright があるとき） |
| `ray/cmdlog.py` | Q&A を Cmd の Data/voice/ に置いて push |
