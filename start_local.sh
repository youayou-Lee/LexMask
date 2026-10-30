#!/usr/bin/env bash
# LexMask 本地 NVIDIA 开发环境启动脚本（Issue#217）
#
# 三档预设 + 三模型引擎（NER/OCR/LA）各自「本地 GPU / 云端自有实例隧道」二选：
#   ./start_local.sh                       # 默认档：NER+OCR 本地 GPU，LA 关
#   ./start_local.sh --with-visual         # 视觉档：+ 本地 LA（本地 OCR 不起，显存放不下）
#   ./start_local.sh --cloud               # 全云档：仅界面+后端，模型全走云端隧道
#
# 引擎细粒度开关：
#   --ner gpu|cloud        NER（HaS vLLM 8080）   默认 gpu
#   --ocr gpu|cloud        OCR（PaddleOCR-VL 8082）默认 gpu
#   --with-visual          本地 LA 视觉检测（8090），默认关
#   --visual cloud         LA 走云端隧道，默认关
#
# 云端 = 自有实例的 SSH 隧道（转发现役端口），不是第三方云 API。
# 隧道由你自己开（命令见私有辅仓 deploy 文档）；本脚本只做连通性预检。
# 地址来源：环境变量或仓库根 .env 的 OCR_BASE_URL / VISUAL_FEATURES_BASE_URL /
# HAS_TEXT_VLLM_BASE_URL；--xx cloud 而对应地址未配置时立即报错，不起半残栈。
#
# 红线（Issue#217）：脚本不内嵌任何地址/口令；真实案卷禁止发往第三方云服务。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

NER_MODE="gpu"
OCR_MODE="gpu"
VISUAL_MODE="off"   # off | local | cloud
WITH_MINERU=0
VRAM_GB="${LEXMASK_VRAM_GB:-12}"

usage() { sed -n '2,30p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ner)         NER_MODE="${2:?--ner 需要 gpu|cloud}"; shift 2 ;;
    --ocr)         OCR_MODE="${2:?--ocr 需要 gpu|cloud}"; shift 2 ;;
    --with-visual) VISUAL_MODE="local"; shift ;;
    --visual)      VISUAL_MODE="${2:?--visual 需要 cloud}"; shift 2 ;;
    --cloud)       NER_MODE="cloud"; OCR_MODE="cloud"; VISUAL_MODE="cloud"; shift ;;
    --with-mineru) WITH_MINERU=1; shift ;;
    -h|--help)     usage ;;
    *) echo "未知参数: $1（--help 查看用法）" >&2; exit 2 ;;
  esac
done

err()  { echo "[start_local] 错误：$*" >&2; exit 1; }
warn() { echo "[start_local] 警告：$*"; }
info() { echo "[start_local] $*"; }

[[ "$NER_MODE" == "gpu" || "$NER_MODE" == "cloud" ]] || err "--ner 只接受 gpu|cloud"
[[ "$OCR_MODE" == "gpu" || "$OCR_MODE" == "cloud" ]] || err "--ocr 只接受 gpu|cloud"
[[ "$VISUAL_MODE" == "off" || "$VISUAL_MODE" == "local" || "$VISUAL_MODE" == "cloud" ]] || err "--visual 只接受 cloud"
[[ "$WITH_MINERU" -eq 0 ]] || err "--with-mineru 由 Issue#217 PR③（MinerU sidecar 仓内化）交付，当前版本尚未提供"

# ---- 真机显存探测（先于互斥校验：判据按实际显存算）-------------------------
vram_free_mb() {
  nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0
}
FREE_MB=0
if command -v nvidia-smi >/dev/null 2>&1; then
  FREE_MB="$(vram_free_mb)"
  TOTAL_MB="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -1 || echo 0)"
  TOTAL_GB=$(( TOTAL_MB / 1024 ))
  if [[ "$TOTAL_GB" -lt "$VRAM_GB" ]]; then
    warn "检测到 GPU 显存 ${TOTAL_GB}GB 小于预期 ${VRAM_GB}GB（如确需按 ${TOTAL_GB}GB 校验，export LEXMASK_VRAM_GB=${TOTAL_GB}）"
    VRAM_GB="$TOTAL_GB"
  fi
else
  warn "未检测到 nvidia-smi——本地 GPU 档可能在无 NVIDIA 环境误用"
fi

# ---- 互斥与显存预算（验收 A9④）-------------------------------------------
if [[ "$VISUAL_MODE" == "local" && "$OCR_MODE" == "gpu" ]]; then
  err "本地 LA（≈9GB）与本地 GPU OCR（≈3GB）叠加 NER（≈2GB）超出 ${VRAM_GB}GB 显存——改用 --ocr cloud，或去掉 --with-visual"
fi
if [[ "$VISUAL_MODE" == "local" ]]; then
  warn "视觉档：本地 OCR 不启动（要 OCR 请叠加 --ocr cloud）"
fi

# 预估：NER 2.2 / OCR 3 / LA 9（Issue#217 设计 §5 资源账）
NEED_MB=2200
[[ "$OCR_MODE" == "gpu" ]] && NEED_MB=$((NEED_MB + 3000))
[[ "$VISUAL_MODE" == "local" ]] && NEED_MB=$((NEED_MB + 9000))
if [[ "$FREE_MB" -gt 0 && "$FREE_MB" -lt "$NEED_MB" ]]; then
  warn "显存吃紧：预估需要 $((NEED_MB / 1024))GB，当前空闲 $((FREE_MB / 1024))GB——继续启动，但可能 OOM（验收判据 A2 会盯 restart 计数与日志）"
fi

# ---- 端口冲突探测（验收 E4）----------------------------------------------
PORTS=(8000 3000)
[[ "$NER_MODE" == "gpu" ]] && PORTS+=(8080)
[[ "$OCR_MODE" == "gpu" ]] && PORTS+=(8082)
[[ "$VISUAL_MODE" == "local" ]] && PORTS+=(8090)
for p in "${PORTS[@]}"; do
  if ss -ltnH "sport = :$p" 2>/dev/null | grep -q .; then
    err "端口 $p 已被占用——先停占用方或 ./stop_local.sh"
  fi
done

# ---- 云端地址解析与改写（验收 A9⑤；容器内经 host.docker.internal 访问宿主隧道）----
resolve_cloud_addr() { # $1=变量名 $2=用途
  local var="$1" val=""
  val="${!var:-}"
  if [[ -z "$val" && -f .env ]]; then
    val="$(grep -E "^${var}=" .env | tail -1 | cut -d= -f2- || true)"
  fi
  [[ -n "$val" ]] || err "$2 走云端但未配置地址——先 export ${var}=http://127.0.0.1:<隧道端口>（隧道命令见私有辅仓 deploy 文档）"
  [[ "$val" =~ ^http:// ]] || err "${var}=$val 不是 http:// 地址"
  # 容器内 127.0.0.1 指容器自身：改写为 host-gateway 网关名
  val="${val//127.0.0.1/host.docker.internal}"
  val="${val//localhost/host.docker.internal}"
  export "$var=$val"
  echo "$val"
}

CLOUD_ADDR_NER=""; CLOUD_ADDR_OCR=""; CLOUD_ADDR_VISUAL=""
health_probe() { # $1=url $2=名称
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 3 "$1" >/dev/null 2>&1
  else
    python3 - "$1" <<'PY' 2>/dev/null
import sys, urllib.request
urllib.request.urlopen(sys.argv[1], timeout=3)
PY
  fi
}

if [[ "$NER_MODE" == "cloud" ]]; then
  CLOUD_ADDR_NER="$(resolve_cloud_addr HAS_TEXT_VLLM_BASE_URL "NER")"
  health_probe "${CLOUD_ADDR_NER%/v1}/health" || warn "NER 云端地址暂不可达（隧道没开？）——backend/frontend 照常启动，任务提交时会失败"
fi
if [[ "$OCR_MODE" == "cloud" ]]; then
  CLOUD_ADDR_OCR="$(resolve_cloud_addr OCR_BASE_URL "OCR")"
  health_probe "$CLOUD_ADDR_OCR/health" || warn "OCR 云端地址暂不可达（隧道没开？）——backend/frontend 照常启动，任务提交时会失败"
fi
if [[ "$VISUAL_MODE" == "cloud" ]]; then
  CLOUD_ADDR_VISUAL="$(resolve_cloud_addr VISUAL_FEATURES_BASE_URL "LA")"
  health_probe "$CLOUD_ADDR_VISUAL/health" || warn "LA 云端地址暂不可达（隧道没开？）——backend/frontend 照常启动，勾视觉项的任务会秒级失败"
fi

# ---- 组装 profiles 并起栈 -------------------------------------------------
PROFILES=(ner ocr-gpu)
[[ "$NER_MODE" == "gpu" ]]    || PROFILES=("${PROFILES[@]/ner/}")
[[ "$OCR_MODE" == "gpu" ]]    || PROFILES=("${PROFILES[@]/ocr-gpu/}")
[[ "$VISUAL_MODE" == "local" ]] && PROFILES+=("visual")
COMPOSE_PROFILES_ARGS=()
for p in "${PROFILES[@]}"; do [[ -n "$p" ]] && COMPOSE_PROFILES_ARGS+=(--profile "$p"); done

info "启动档位：NER=${NER_MODE} OCR=${OCR_MODE} LA=${VISUAL_MODE}"
[[ -n "$CLOUD_ADDR_NER" ]]    && info "  NER 云端 → $CLOUD_ADDR_NER"
[[ -n "$CLOUD_ADDR_OCR" ]]    && info "  OCR 云端 → $CLOUD_ADDR_OCR"
[[ -n "$CLOUD_ADDR_VISUAL" ]] && info "  LA  云端 → $CLOUD_ADDR_VISUAL"

docker compose "${COMPOSE_PROFILES_ARGS[@]}" up -d

info "等待 backend 健康…"
for i in $(seq 1 60); do
  if health_probe "http://127.0.0.1:${BACKEND_PORT:-8000}/health"; then
    info "backend 就绪"
    break
  fi
  [[ "$i" -eq 60 ]] && { warn "backend 120s 内未就绪——查看日志：docker compose logs backend"; exit 1; }
  sleep 2
done

command -v nvidia-smi >/dev/null 2>&1 && \
  info "显存当前：$(nvidia-smi --query-gpu=memory.used,memory.total --format=csv,noheader | head -1)"
info "完成 → 浏览器打开 http://localhost:${FRONTEND_PORT:-3000}"
info "停止：./stop_local.sh"
