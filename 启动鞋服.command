#!/bin/bash
# 双击即可：生产模式启动【后端 8020 + 前端 5180】，就绪后自动打开浏览器
cd "$(cd "$(dirname "$0")" && pwd)" || exit 1
echo "=== 鞋服智能设计 Agent · 启动中 ==="

if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then
  echo "请先安装 Node.js，并确认 node 与 npm 在 PATH 中。"
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "请先按 README.md 安装后端依赖，创建 .venv。"
  read -n 1 -s -r -p "按任意键关闭…"
  exit 1
fi

if lsof -nP -iTCP:8020 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "· 后端 8020 已在运行，跳过"
else
  nohup .venv/bin/python -m uvicorn app.main:app --port 8020 > /tmp/shoe-8020.log 2>&1 &
  echo "· 后端启动中（8020）"
fi

if lsof -nP -iTCP:5180 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "· 前端 5180 已在运行，跳过"
else
  if [ ! -d frontend/.next ]; then
    echo "· 首次运行，正在构建前端（约 30 秒）…"
    (cd frontend && npm run build > /tmp/shoe-build.log 2>&1)
  fi
  (cd frontend && nohup npx next start -p 5180 > /tmp/shoe-next.log 2>&1 &)
  echo "· 前端启动中（5180，生产模式）"
fi

for i in $(seq 1 40); do
  if curl -s -m 2 -o /dev/null http://127.0.0.1:5180/ && curl -s -m 2 -o /dev/null http://127.0.0.1:8020/api/health; then
    echo "· 就绪 ✅  页面：http://localhost:5180/"
    open "http://localhost:5180/"
    echo ""
    echo "（关掉服务：双击「停止鞋服.command」）"
    read -n 1 -s -r -p "按任意键关闭这个窗口…"
    exit 0
  fi
  sleep 1
done
echo "⚠️ 启动超时。可看日志：/tmp/shoe-8020.log 与 /tmp/shoe-next.log"
read -n 1 -s -r -p "按任意键关闭…"
