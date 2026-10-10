#!/bin/bash
echo "=== 停止鞋服智能设计 Agent ==="
for port in 8020 5180; do
  PID=$(lsof -nP -iTCP:$port -sTCP:LISTEN -t 2>/dev/null)
  if [ -n "$PID" ]; then kill $PID 2>/dev/null && echo "· 已停止端口 $port"; else echo "· 端口 $port 本来就没在跑"; fi
done
sleep 1
echo "完成。"
read -n 1 -s -r -p "按任意键关闭…"
