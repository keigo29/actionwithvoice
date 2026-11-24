#!/bin/zsh
# Ray.app を作る（ダブルクリックで起動できるようにする）
#
#   ./make-app.sh            # ~/Applications/Ray.app を作る
#   ./make-app.sh <置き場所>  # 置き場所を変える
#
# - ダブルクリックすると、iTerm（なければターミナル）の新しい窓で python -m ray を動かす
#   （マイク・入力監視の許可は、そのアプリのものを使う。RAY_TERMINAL=Terminal でターミナルにする）
# - すでに動いていれば、もう1つは起動せず、状態の画面を開くだけ
# - 止めるときは、その窓で Ctrl+C（窓を閉じても止まる）
# - このフォルダを動かしたら、もう一度これを動かす
set -euo pipefail

DESK="${0:A:h}"
DEST="${1:-$HOME/Applications}/Ray.app"
PY="$DESK/.venv/bin/python"

if [[ ! -x "$PY" ]]; then
  echo "❌ $PY がありません。README の「はじめの設定」で .venv を作ってください" >&2
  exit 1
fi

# 状態の画面のポート（config.toml の [ui] port）
PORT=$(cd "$DESK" && "$PY" -c 'from ray import config; print(config.load(None)["ui"]["port"])')

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cat > "$WORK/ray.applescript" <<EOF
set desk to "$DESK"
set url_ to "http://localhost:$PORT/"
-- すでに動いていれば、画面を開くだけ
try
	do shell script "/usr/bin/curl -s -o /dev/null -m 1 " & quoted form of url_
	open location url_
	return
end try
set cmd to "cd " & quoted form of desk & " && clear && exec .venv/bin/python -m ray"
EOF
# 入力監視・マイクの許可が出ているアプリで動かす（iTerm があれば iTerm、なければターミナル）
if [[ "${RAY_TERMINAL:-}" != "Terminal" && -d /Applications/iTerm.app ]]; then
  cat >> "$WORK/ray.applescript" <<'EOF'
tell application "iTerm"
	activate
	set w to (create window with default profile)
	tell current session of w to write text cmd
end tell
EOF
else
  cat >> "$WORK/ray.applescript" <<'EOF'
tell application "Terminal"
	activate
	do script cmd
end tell
EOF
fi

mkdir -p "${DEST:h}"
rm -rf "$DEST"
osacompile -o "$DEST" "$WORK/ray.applescript"

# アイコン：Ray の光る円（外の道具を使わず、Python で PNG を描く）
"$PY" - "$WORK/Ray.iconset" <<'PY'
import math, struct, sys, zlib
from pathlib import Path

out = Path(sys.argv[1]); out.mkdir()

def png(size: int) -> bytes:
    rows = []
    c = (size - 1) / 2
    for y in range(size):
        row = bytearray([0])
        for x in range(size):
            d = math.hypot(x - c, y - c) / (size / 2)   # 中心からの距離（端で 1）
            # 角の丸い四角の地（macOS のアイコンの形）
            m = 0.80 * size / 2; r = 0.18 * size
            qx = max(abs(x - c) - (m - r), 0); qy = max(abs(y - c) - (m - r), 0)
            inside = math.hypot(qx, qy) <= r
            if not inside:
                row += bytes(4); continue
            ring = math.exp(-((d - 0.52) / 0.035) ** 2)          # 光る輪
            glow = 0.45 * math.exp(-((d - 0.52) / 0.12) ** 2)    # 輪のまわりのにじみ
            core = 0.35 * math.exp(-(d / 0.18) ** 2)             # 中の光
            v = min(ring + glow + core, 1.0)
            bg = (8, 14, 28)
            col = (90, 200, 255)
            px = [round(b + (k - b) * v) for b, k in zip(bg, col)]
            row += bytes(px + [255])
        rows.append(bytes(row))
    raw = b"".join(rows)
    def chunk(t, d): return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))

for s in (16, 32, 128, 256, 512):
    (out / f"icon_{s}x{s}.png").write_bytes(png(s))
    (out / f"icon_{s}x{s}@2x.png").write_bytes(png(s * 2))
PY
iconutil -c icns "$WORK/Ray.iconset" -o "$DEST/Contents/Resources/applet.icns"
# 既定のアイコン（Assets.car）があると、そちらが優先されるので外す
rm -f "$DEST/Contents/Resources/Assets.car"
/usr/libexec/PlistBuddy -c "Delete :CFBundleIconName" "$DEST/Contents/Info.plist" 2>/dev/null || true
# 中身を変えたので、署名をし直す（しないと「壊れている」と言われる）
codesign --force --deep -s - "$DEST" 2>/dev/null
touch "$DEST"

echo "✅ $DEST を作りました（ダブルクリックで起動。Dock に入れておくと便利です）"
