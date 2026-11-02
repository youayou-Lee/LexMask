#!/usr/bin/env bash
# LexMask 本地开发环境停止（Issue#217）
#
# 覆盖所有可能被 start_local.sh 启用的 profile，全部落下并清理孤儿容器。
# 不删数据卷（backend-data / backend-uploads / backend-outputs 保留）。

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

export COMPOSE_PROFILES="gpu,ner,ocr-gpu,visual,mineru"
docker compose down --remove-orphans
echo "[stop_local] 已停止（数据卷保留；模型权重不受影响）"
