#!/usr/bin/env bash
# 车票 / 机票 比价搜索 —— Linux / macOS 启动脚本
set -euo pipefail
cd "$(dirname "$0")"

PORT="${1:-8765}"

if ! command -v python3 >/dev/null 2>&1; then
  echo "[错误] 未找到 python3，请先安装 Python 3.10+"
  exit 1
fi

echo "正在启动「车票 / 机票 比价搜索」... 端口 $PORT"
exec python3 -m farecompare serve --port "$PORT"
