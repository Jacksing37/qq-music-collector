#!/usr/bin/env bash
# QQ 音乐收集机器人 · 启动脚本
# 用法：
#   直接跑：  ./deploy/start.sh
#   后台跑：  nohup ./deploy/start.sh > bot.log 2>&1 &
#   systemd：ExecStart 指向本文件即可（见 qq-music-collector.service）
set -e

# 切到项目根目录（本文件位于 <项目>/deploy/start.sh）
cd "$(dirname "$0")/.."

# 没有虚拟环境就现场建一个并装依赖
if [ ! -x .venv/bin/python ]; then
  echo "[start.sh] 未检测到 .venv，正在创建并安装依赖…"
  python3 -m venv .venv
  .venv/bin/python -m pip install -U pip
  .venv/bin/python -m pip install -r requirements.txt
fi

export PYTHONUNBUFFERED=1
exec .venv/bin/python bot.py
