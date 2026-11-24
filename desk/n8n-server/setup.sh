#!/bin/bash
# サーバー（Ubuntu）で1回だけ動かす：Docker と Tailscale を入れて、n8n を起動する
#   bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  echo "== Docker を入れます"
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

if ! command -v tailscale >/dev/null; then
  echo "== Tailscale を入れます"
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "== Tailscale にログインします（出てきた URL をブラウザで開いて、Mac と同じアカウントで）"
  sudo tailscale up
fi

if [[ ! -f .env ]]; then
  cp .env.example .env
  sed -i "s/^N8N_ENCRYPTION_KEY=$/N8N_ENCRYPTION_KEY=$(openssl rand -hex 32)/" .env
  echo "❗ .env を作りました。N8N_HOST と TUNNEL_TOKEN を書いてから、もう一度 bash setup.sh してください"
  exit 1
fi
if grep -q -E '^(TUNNEL_TOKEN=|N8N_HOST=n8n\.example\.com)$' .env; then
  echo "❗ .env の N8N_HOST と TUNNEL_TOKEN を書いてください" >&2
  exit 1
fi

mkdir -p data
sudo chown -R 1000:1000 data   # n8n はコンテナの中で uid 1000 で動く

sudo docker compose up -d
echo "✅ 起動しました。https://$(grep '^N8N_HOST=' .env | cut -d= -f2)/ を開いて、最初のアカウントを作ってください"
echo "   Ray への知らせが届くか：sudo docker compose exec n8n wget -qO- --post-data='{}' http://<Mac の Tailscale の IP>:8765/notify"
echo "   （「401」などのエラー番号が出れば、道はつながっています。timed out や refused なら届いていません）"
