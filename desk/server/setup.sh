#!/bin/bash
# サーバー（Ubuntu。Oracle の無料枠など）で、Ray を iPhone から使えるようにする。何度動かしても大丈夫
#   bash desk/server/setup.sh
# 先に済ませておくこと（README.md の 1〜2）：Cmd のクローン（push できる状態）と、claude のログイン
set -euo pipefail
DESK="$(cd "$(dirname "$0")/.." && pwd)"
CMD_DIR="${CMD_DIR:-$HOME/Cmd}"
PORT=8765

echo "== 必要なものを入れます"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv git curl
if ! command -v node >/dev/null || [[ "$(node -v | tr -d v | cut -d. -f1)" -lt 20 ]]; then
  curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
  sudo apt-get install -y -qq nodejs
fi
command -v claude >/dev/null || sudo npm install -g @anthropic-ai/claude-code
if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
if ! tailscale status >/dev/null 2>&1; then
  echo "== Tailscale にログインします（出てきた URL をブラウザで開いて、iPhone と同じアカウントで）"
  sudo tailscale up
fi

cd "$DESK"
[[ -d .venv ]] || python3 -m venv .venv
.venv/bin/pip install -q -r server/requirements.txt

if [[ ! -d "$CMD_DIR/.git" ]]; then
  echo "❗ $CMD_DIR に Cmd のクローンがありません。README.md の「1. Cmd を置く」をしてから、もう一度動かしてください" >&2
  exit 1
fi
if [[ ! -f "$HOME/.claude/.credentials.json" ]]; then
  echo "❗ Claude Code にログインしていません。claude を起動してログインしてから（README.md の「2」）、もう一度動かしてください" >&2
  exit 1
fi
if [[ ! -f config.toml ]]; then
  cp server/config.example.toml config.toml
  TOKEN="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  sed -i "s|^token = \"\"|token = \"$TOKEN\"|; s|^cmd_dir = .*|cmd_dir = \"$CMD_DIR\"|" config.toml
  chmod 600 config.toml
  echo "== config.toml を作りました。iPhone で開くときの合言葉：$TOKEN"
  echo "   （Gemini や Tavily の鍵があれば、config.toml に書いてから sudo systemctl restart ray）"
fi

echo "== Ray を常駐させます（止まっても自動で起動し直す）"
sudo tee /etc/systemd/system/ray.service >/dev/null <<UNIT
[Unit]
Description=Ray（サーバー。iPhone から使う）
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DESK
ExecStart=$DESK/.venv/bin/python -m ray --server
Environment=PATH=/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT
sudo systemctl daemon-reload
sudo systemctl enable --now ray
sudo systemctl restart ray

echo "== Tailscale の中だけに、https で開きます"
sudo tailscale serve --bg "$PORT"
URL="$(tailscale serve status 2>/dev/null | grep -o 'https://[^ ]*' | head -1 || true)"
echo "✅ できました。iPhone（Tailscale をつないだ状態）の Safari で開いてください：${URL:-（sudo tailscale serve status で確かめる）}"
echo "   合言葉は config.toml の [server] token。開けたら、共有 →「ホーム画面に追加」"
echo "   ようすを見る：journalctl -u ray -f"
