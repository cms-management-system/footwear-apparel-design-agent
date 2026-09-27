#!/bin/bash
# 把「启动 / 停止」做成两个 macOS 应用图标（放在 ~/Applications），从桌面撤走。
#
# 用法：bash scripts/install_launcher_apps.sh
# 之后：Spotlight（Cmd+空格）输入「鞋服」即可启动；或把 ~/Applications 里的图标拖到 Dock。
#
# 说明：脚本自身位置反推项目目录，不依赖当前工作目录；重复执行会覆盖重建，安全幂等。
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
APPS_DIR="$HOME/Applications"
NODE_BIN="$HOME/.local/node/bin"

make_app() {          # $1=应用名  $2=可执行脚本内容
  local name="$1" body="$2"
  local app="$APPS_DIR/$name.app"
  rm -rf "$app"
  mkdir -p "$app/Contents/MacOS"
  cat > "$app/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>$name</string>
  <key>CFBundleDisplayName</key><string>$name</string>
  <key>CFBundleExecutable</key><string>run</string>
  <key>CFBundleIdentifier</key><string>local.shoestyle.launcher.$(printf '%s' "$name" | shasum | cut -c1-8)</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
</dict></plist>
PLIST
  printf '%s\n' "$body" > "$app/Contents/MacOS/run"
  chmod +x "$app/Contents/MacOS/run"
  echo "已生成：$app"
}

make_app "鞋服-启动工作台" "$(cat <<EOF
#!/bin/bash
# 鞋服智能设计 Agent · 启动（生产模式：后端 8020 + 前端 5180），就绪后自动打开浏览器
export PATH="$NODE_BIN:\$PATH"
PROJECT_DIR="$PROJECT_DIR"
cd "\$PROJECT_DIR" || { osascript -e 'display alert "找不到项目文件夹"' ; exit 1; }
notify() { osascript -e "display notification \\"\$1\\" with title \\"鞋服设计工作台\\"" >/dev/null 2>&1 || true; }

if lsof -nP -iTCP:8020 -sTCP:LISTEN >/dev/null 2>&1; then :; else
  nohup .venv/bin/python -m uvicorn app.main:app --port 8020 > /tmp/shoe-8020.log 2>&1 &
fi
if lsof -nP -iTCP:5180 -sTCP:LISTEN >/dev/null 2>&1; then :; else
  if [ ! -d frontend/.next ]; then
    notify "首次运行，正在构建前端（约 30 秒）…"
    (cd frontend && npm run build > /tmp/shoe-build.log 2>&1)
  fi
  (cd frontend && nohup npx next start -p 5180 > /tmp/shoe-next.log 2>&1 &)
fi

for i in \$(seq 1 40); do
  if curl -s -m 2 -o /dev/null http://127.0.0.1:5180/ && curl -s -m 2 -o /dev/null http://127.0.0.1:8020/api/health; then
    notify "已就绪，正在打开工作台"
    open "http://localhost:5180/"
    exit 0
  fi
  sleep 1
done
notify "启动超时：可看 /tmp/shoe-8020.log 与 /tmp/shoe-next.log"
EOF
)"

make_app "鞋服-停止服务" "$(cat <<'EOF'
#!/bin/bash
# 鞋服智能设计 Agent · 停止（释放 8020 / 5180）
lsof -nP -iTCP:8020 -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null || true
lsof -nP -iTCP:5180 -sTCP:LISTEN -t 2>/dev/null | xargs -r kill 2>/dev/null || true
sleep 1
osascript -e 'display notification "后端与前端已停止，端口已释放" with title "鞋服设计工作台"' >/dev/null 2>&1 || true
EOF
)"

echo
echo "完成。打开方式（不用放桌面）："
echo "  1) Spotlight：按 Cmd + 空格，输入「鞋服」，回车"
echo "  2) 或打开「应用程序」文件夹里的这两个图标，拖到 Dock 上"
