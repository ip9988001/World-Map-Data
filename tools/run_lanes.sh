#!/usr/bin/env bash
# 串行跑完 16 条泳道（本机 2 核 2GB：泳道并行会打爆，故串行 + 窗口预算 + 断点续跑）
# 磁盘 2GB 护栏由 collectors/pipeline.py 内置，触顶自动停止开新源。
set -u
cd "$(dirname "$0")/.."
PLAN=${PLAN:-data/泳道计划.json}
WINDOW=${WINDOW:-1200}
LANES=${LANES:-16}
for k in $(seq 0 $((LANES-1))); do
  echo "===== lane $k 起跑 $(date '+%F %T') ====="
  python3 -m collectors.pipeline --plan "$PLAN" --lane "$k" --max-seconds "$WINDOW"
  echo "===== lane $k 结束 $(date '+%F %T') ====="
done
echo "ALL LANES DONE $(date '+%F %T')"
