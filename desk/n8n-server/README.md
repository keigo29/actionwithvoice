# n8n をサーバーに置く（無料）

n8n を Mac ではなくサーバーで動かします。Mac を閉じていても、決まった時刻のワークフローや Webhook が動きます。

```
ブラウザ ──https──▶ Cloudflare ──Tunnel──▶ サーバーの n8n
                                              │
                                   Tailscale（外から見えない道）
                                              ▼
                                  Mac の Ray（POST /notify だけ受ける）
```

| 使うもの | 役目 | 料金 |
|---|---|---|
| Oracle Cloud Always Free（ARM の VM） | n8n を動かすサーバー | 無料（登録に本人確認のカードが要る） |
| Cloudflare Tunnel | `https://n8n.<自分のドメイン>` を n8n につなぐ。サーバーのポートは開けない | 無料（Cloudflare に入れたドメインが要る） |
| Tailscale | サーバーの n8n から Mac の Ray に知らせを送る | 無料（個人なら100台まで） |

## 1. サーバーを作る（Oracle Cloud）

1. https://www.oracle.com/cloud/free/ で登録する。ホームリージョンは **Japan East (Tokyo)** か **Japan Central (Osaka)**（あとから変えられない）
2. Compute → Instances → Create instance
   - Image：**Ubuntu 24.04**
   - Shape：**VM.Standard.A1.Flex**（Ampere・ARM）。OCPU 1〜2、メモリ 6〜12 GB で十分（無料枠は合計 4 OCPU・24 GB まで）
   - 「Out of capacity」と出たら、時間をおいて作り直すか、Availability domain を変える
   - SSH キー：「Save private key」で保存する（または Mac の `~/.ssh/id_ed25519.pub` を貼る）
3. できたら Public IP を控えて、Mac から入れるか確かめる：`ssh ubuntu@<Public IP>`

ポートは SSH（22）のほかに開ける必要はありません。

## 2. Cloudflare Tunnel を作る

1. Cloudflare のダッシュボード → Zero Trust → Networks → Tunnels → **Create a tunnel** →「Cloudflared」
2. 名前（`n8n` など）をつける → 環境は **Docker** を選ぶ → 出てくるコマンドの `--token` のあとの長い文字列を控える（これが `TUNNEL_TOKEN`）
3. Public Hostname を足す
   - Subdomain：`n8n`、Domain：自分のドメイン
   - Service：**HTTP**、URL：**`n8n:5678`**

## 3. サーバーで n8n を起動する

```sh
# Mac から、このフォルダをサーバーに送る
scp -r desk/n8n-server ubuntu@<Public IP>:~/

# サーバーで
ssh ubuntu@<Public IP>
cd ~/n8n-server
bash setup.sh        # Docker と Tailscale を入れる。Tailscale のログイン URL が出たら開く
nano .env            # N8N_HOST と TUNNEL_TOKEN を書く（暗号化の鍵は自動で入る）
bash setup.sh        # もう一度。n8n と Tunnel が起動する
```

`https://n8n.<自分のドメイン>/` を開いて、最初のアカウント（オーナー）を作ります。**ここで作るまでは、誰でもオーナーになれてしまう**ので、起動したらすぐに作ってください。

- `.env` の `N8N_ENCRYPTION_KEY` は変えないでください（変えると保存した資格情報が読めなくなります）。念のため、パスワード管理などに控えておくと安心です
- 止める・起動し直す：`sudo docker compose down`／`sudo docker compose up -d`
- 版を上げる：`docker-compose.yml` の `n8n:2.13.4` を変えて `sudo docker compose pull && sudo docker compose up -d`
- 記録を見る：`sudo docker compose logs -f n8n`

## 4. Mac の Ray に知らせが届くようにする

1. Mac に Tailscale を入れて、サーバーと同じアカウントでログインする（App Store か https://tailscale.com/download ）
2. Mac の Tailscale の IP を調べる：メニューバーの Tailscale のアイコン → 自分の Mac の IP（`100.` で始まる）
3. `desk/config.toml` の `[notify]` に足す
   ```toml
   [notify]
   enabled = true
   token = "（16文字以上の合言葉）"
   listen = "100.x.y.z"     # 2 で調べた Mac の Tailscale の IP
   ```
4. Ray を起動し直す。`外からの知らせも受けます：POST http://100.x.y.z:8765/notify` と出れば OK（`python -m ray --check` でも確かめられます）
   - macOS が「受信接続を許可しますか？」と聞いてきたら「許可」
5. サーバーで道がつながっているか確かめる
   ```sh
   sudo docker compose exec n8n wget -qO- --post-data='{}' http://100.x.y.z:8765/notify
   ```
   「401」などのエラー番号が出れば、つながっています（合言葉を付けていないので断られるのが正しい）

Tailscale の道から受けるのは `POST /notify` だけです。状態の画面（会話の中身が出る）は、これまでどおり Mac の中からしか見えません。

## 5. ワークフローを入れる

1. n8n の画面で「Import from File」→ `desk/examples/n8n/ray-notify.json`
2. HTTP Request ノードを直す
   - URL：`http://127.0.0.1:8765/notify` → **`http://100.x.y.z:8765/notify`**（Mac の Tailscale の IP）
   - `X-Ray-Token`：`config.toml` の合言葉
3. 動かしてみて、Ray が話したら Active にする

## 6. Mac の n8n を止める

サーバーで動いたら、Mac の n8n（`n8n start`）は止めます。両方で動いていると、同じワークフローが2回動きます。

## 気をつけること

- **Mac がスリープ中・Ray が止まっているとき**は、知らせは届きません（n8n の実行は失敗の記録になります）
- Tailscale の IP は、その Mac を消して登録し直さないかぎり変わりません
- Oracle の無料枠は、長いあいだ CPU をほとんど使わないと「使われていない」とみなされて止められることがあります。そうなったら、Oracle の画面から起動し直してください
- n8n の画面は誰でも開けるので、オーナーのパスワードは長くしてください。もっと固くしたいときは、Cloudflare Zero Trust の Access で自分のメールだけ通すようにできます（Webhook を使うなら、`/webhook/` は Access の対象から外す）
