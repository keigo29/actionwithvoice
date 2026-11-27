# サーバーの Ray（iPhone から、Mac を通さずに使う）

Ray の頭をサーバー（Oracle の無料枠など）に置いて、iPhone の Safari から Ray の画面を開いて使います。Mac が寝ていても使えます。

```
iPhone の Safari（Ray の画面。プレゼン形式）
  → 話すボタン：iPhone が聞き取って文字にする
  → Tailscale の中だけの道（https）で、サーバーへ
  → サーバーの Ray：トークンを使わない頼みごと → Gemini → Claude Code（Pro の枠）
  → 1文ずつ、その文の場面（予定・グラフ・図）と一緒に iPhone へ
  → iPhone が、その文を読み上げながら、その場面を出す
```

- 頭は机の Mac の Ray と同じコードです（`python -m ray --server`）。振り分け・決まった頼みごと・朝のまとめ・図解・グラフも同じです
- 聞き取りと読み上げは iPhone がします（Whisper・VOICEVOX は使いません）
- 入口は Tailscale の中だけで、さらに合言葉でログインした人だけです。インターネットには開きません
- Ray のデータはサーバーに置きません（Cmd は GitHub にあります）。サーバーが消えても、この手順でほかの場所に作り直せます

## 0. 用意するもの

- Ubuntu のサーバー（n8n を置いた Oracle の無料枠のサーバーでよい。[`../n8n-server/README.md`](../n8n-server/README.md) の「1」）
- iPhone に Tailscale のアプリ（サーバーと同じアカウントでログイン）
- Tailscale の管理画面 → DNS で、**MagicDNS** と **HTTPS Certificates** をオンにする（iPhone で https で開くため。https でないと、Safari の音声入力が使えません）

## 1. Cmd を置く（push できる状態に）

サーバーから Cmd に push できるよう、Cmd だけに書き込める鍵を作ります。

```sh
ssh-keygen -t ed25519 -f ~/.ssh/cmd -N ""
cat ~/.ssh/cmd.pub
```

出てきた1行を、GitHub の Cmd リポジトリ → Settings → Deploy keys → Add deploy key に貼り、**Allow write access** にチェックします。

```sh
cat >> ~/.ssh/config <<'EOF'
Host github-cmd
  HostName github.com
  IdentityFile ~/.ssh/cmd
EOF
git clone git@github-cmd:keigo29/Cmd.git ~/Cmd
git -C ~/Cmd config user.name "Ray (server)"
git -C ~/Cmd config user.email "ray@localhost"
```

## 2. このリポジトリと Claude Code

```sh
git clone https://github.com/keigo29/actionwithvoice.git ~/actionwithvoice
sudo npm install -g @anthropic-ai/claude-code   # Node が無ければ、先に「3」を1回動かすと入る
claude                                          # 出てきた URL をブラウザで開いて、Pro のアカウントでログイン。できたら /exit
```

## 3. 入れて、常駐させる

```sh
bash ~/actionwithvoice/desk/server/setup.sh
```

- 必要なもの（Python・Node・Claude Code・Tailscale）を入れ、`desk/config.toml` を作ります。**iPhone で開くときの合言葉**が表示されます（`config.toml` の `[server] token`）
- Ray を systemd で常駐させます（止まっても自動で起動し直す。サーバーを起動し直しても動く）
- `tailscale serve` で、Tailscale の中だけに https で開きます。最後に iPhone で開くアドレス（`https://サーバーの名前.〜.ts.net/`）が出ます
- Gemini や Tavily の鍵は `desk/config.toml` に書いて、`sudo systemctl restart ray`（書き方は [`config.example.toml`](config.example.toml)）

## 4. iPhone で開く

1. Tailscale をつないだ状態で、Safari でアドレスを開く
2. 合言葉を入れて「ひらく」（1年間は入れ直さなくてよい）
3. 共有 → **ホーム画面に追加**。次からはアプリのように開ける

## 使い方

- 左のマイクを押して話す（もう一度押すか、黙ると止まる）。キーボードで書いてもよい
- 右の四角で、話している途中でも止める
- 「次」「戻って」「もう一回」「おはよう」「今日の予定は？」「牛乳を買うをやることに追加して」など、机の Ray と同じように使えます
- 声は iPhone の声です（設定 → アクセシビリティ → 読み上げコンテンツ → 声 → 日本語 で、きれいな声を入れると変わります）

## 気をつけること

- **机の Mac の Ray とは、別々に動きます**（会話の続きは引き継がない）。Cmd には両方が書くので、どちらも書く前に `git pull` し、push はまとめて送ります
- **Claude の5時間の枠**は、Mac・サーバー・ふだんの Claude Code で分け合います
- 朝のまとめ・予定の前の知らせ・夜のふりかえり（Ray から声をかける）は、机の Mac の Ray だけが行います。サーバーでは「おはよう」と言えば朝のまとめを話します
- サーバーが止まっているときは、iPhone のショートカット「聞く」（声と文字だけ）が使えます
- ようすを見る：`journalctl -u ray -f`。止める：`sudo systemctl stop ray`。コードを新しくしたら：`git -C ~/actionwithvoice pull && sudo systemctl restart ray`
