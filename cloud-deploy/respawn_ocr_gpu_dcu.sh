#!/usr/bin/env bash
# OCR GPU 服务 —— DTK25.04.2 镜像专用（paddle-dcu 3.2.1 = GPU 唯一验证线）
# 2026-09-10 dcu-bw 实测: DTK25.04.2+gfx926 conv/pool PASS, PPStructureV3 GPU 可用
# Issue#169: 参数化 OCR_PORT/窗口名/就绪判定 —— 同机第二实例用法:
#   OCR_PORT=18092 bash respawn_ocr_gpu_dcu.sh   （窗口名 ocr2；等待 /health 且 ready:true 才放行）
# 注意（门①.5 C2）：B 模式（backend 双端点分发）下两个实例都【不要】设 OCR_PEER_URL——
# peer char 委托与 backend 分发是两套并行机制，叠加会互抢锁且 kill 场景静默退化。
set -uo pipefail
P=/root/private_data/redaction-persist
BACKEND=/root/redaction/LexMask/backend
LOG_DIR=/root/redaction/cloud-deploy/logs; mkdir -p "$LOG_DIR"
SESSION=redaction
PORT="${OCR_PORT:-8082}"
if [ "$PORT" = "8082" ]; then WIN="ocr"; LOG="ocr-gpu.log"; else WIN="ocr2"; LOG="ocr2-gpu.log"; fi
: '${PLATFORM_PROXY_URL:?export PLATFORM_PROXY_URL=平台代理}'; export http_proxy="$PLATFORM_PROXY_URL"
export https_proxy="$http_proxy"
export no_proxy='localhost,127.0.0.1,0.0.0.0' NO_PROXY='localhost,127.0.0.1,0.0.0.0'
PROXY_EXPORT="export http_proxy='$http_proxy' https_proxy='$https_proxy' no_proxy=localhost,127.0.0.1,0.0.0.0;"
DTK_EXPORT="set +u; source /opt/dtk/env.sh; set -u;"

tmux has-session -t "$SESSION" 2>/dev/null || tmux new-session -d -s "$SESSION" -n init "sleep infinity"
tmux kill-window -t "$SESSION:$WIN" 2>/dev/null || true
tmux new-window -d -t "$SESSION" -n "$WIN" "$PROXY_EXPORT $DTK_EXPORT cd '$BACKEND' && \
  HIP_VISIBLE_DEVICES=0 OCR_VL_ENABLED=0 OCR_REQUIRE_GPU=true OCR_DEVICE=dcu:0 FLAGS_conv_workspace_size_limit=2000 \
  OCR_STRUCTURE_ENABLED=1 OCR_STRUCTURE_PRIMARY=1 OCR_STRUCTURE_WARMUP=1 \
  OCR_STRUCTURE_RELEASE_AFTER_REQUEST=0 OCR_MAX_IMAGE_SIDE=2048 OCR_MAX_NEW_TOKENS=2048 \
  PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True PADDLE_PDX_DISABLE_DEV_MODEL_WL=true \
  PADDLE_PDX_CACHE_HOME=$P/paddlex-cache \
  PYTHONPATH='$BACKEND' OCR_PORT=$PORT \
  $P/dot-venvs/paddle-25041/bin/python '$BACKEND'/scripts/ocr_server.py 2>&1 | tee -a $LOG_DIR/$LOG"

# 就绪判定（门①.5 I8）：HTTP 200 且 payload ready==true——避免模型未就绪的早期请求
# 进入 backend 轮询池污染故障计数。busy/loading 视为未就绪继续等。
w=0; printf "等 OCR-GPU(paddle-dcu 3.2.1, port $PORT) "
until curl -sf -m 3 "http://127.0.0.1:$PORT/health" 2>/dev/null | grep -q '"ready": *true'; do
  sleep 5; w=$((w+5)); printf "."
  [ $w -ge 900 ] && { echo " 超时"; tail -30 "$LOG_DIR/$LOG"; exit 1; }
done
echo " OK (${w}s)"
curl -s -m 5 "http://127.0.0.1:$PORT/health"; echo
