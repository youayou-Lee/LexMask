#!/usr/bin/env bash
# 新旧同题背靠背对照（AC1/AC2，云实例人触发，门⑤）
# 用法：ENV_LABEL/HARDWARE/T2_DATA_DIR 就绪后
#   bash eval-ng/scripts/back_to_back.sh hardcase http://127.0.0.1:8080/v1 [model]
#
# 对齐参数清单（新旧逐项一致——同题同分成立的前提，改任何一项须两线同步）：
#   1. 类型传法：逐条按当页 GT 类型派生 types = sorted(e["entities"])   （两线同）
#   2. max_tokens：两线 4000（新 HasParticipant 构造默认 = 旧 HttpEngine 默认）
#   3. timeout：两线 120s
#   4. grouping：两线 off（新引擎不分组；旧线不传分组参数）
#   5. temperature/top_p：两线同走 nerq 内置常量
#   6. 同实例同会话背靠背执行 + N=3 取聚合（LLM 端点抖动纪律，设计 §3）
set -euo pipefail

EXAM="${1:?考卷参数必填（桶名，如 hardcase）}"
NER_BASE="${2:?ner-base 必填（如 http://127.0.0.1:8080/v1）}"
MODEL="${3:-}"
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_DIR="${REPO}/eval-ng/reports/back-to-back-${TS}"
RUNS=3
mkdir -p "$OUT_DIR"
: "${T2_DATA_DIR:?须导出 T2_DATA_DIR（T2 桶 jsonl 根目录）}"
: "${ENV_LABEL:?须导出 ENV_LABEL}"
: "${HARDWARE:?须导出 HARDWARE}"
TOPOLOGY="${TOPOLOGY:-scnet-main}"
ENGINE_ARG="has=${NER_BASE}"
[ -n "$MODEL" ] && ENGINE_ARG="${ENGINE_ARG},model=${MODEL}"

echo "== [1/2] 旧引擎 benchmark_t2（N=${RUNS}）=="
for i in $(seq 1 "$RUNS"); do
  python "${REPO}/eval/benchmarks/t2/benchmark_t2.py" \
    --data-dir "${T2_DATA_DIR}" \
    --engine "${ENGINE_ARG}" \
    --buckets "$EXAM" \
    --out "$OUT_DIR/old-run$i" >/dev/null
done

echo "== [2/2] 新引擎 eval-ng（N=${RUNS}）=="
python "${REPO}/eval-ng/scripts/make_exam_from_t2.py" \
  --data-dir "${T2_DATA_DIR}" --bucket "$EXAM" --out "$OUT_DIR/exam.json"
for i in $(seq 1 "$RUNS"); do
  EXTRA=()
  [ -n "$MODEL" ] && EXTRA+=(--model "$MODEL")
  python "${REPO}/eval-ng/cli.py" run "$OUT_DIR/exam.json" \
    --exam-id "$EXAM" \
    --participant has --ner-base "$NER_BASE" "${EXTRA[@]+"${EXTRA[@]}"}" \
    --runs 1 \
    --env-label "$ENV_LABEL" \
    --target-label "evalng-b2b-run$i" \
    --hardware "$HARDWARE" \
    --topology "$TOPOLOGY" \
    --stack-version "ner-base=${NER_BASE}" \
    --out "$OUT_DIR/new-run$i" >/dev/null \
    || echo "[warn] 新引擎第 $i 轮闸门 FAIL（hardcase 桶数字闸预期不过；AC1 比较 P/R/F1 一致性）"
done

echo "== 汇总（P/R/F1 对照）=="
python "${REPO}/eval-ng/scripts/compare_back_to_back.py" \
  --old "$OUT_DIR"/old-run*/[0-9]*-t2-benchmark.json \
  --new "$OUT_DIR"/new-run*/reports/*.json \
  --exam "$EXAM" --out "$OUT_DIR/summary.md"
echo "完成：$OUT_DIR/summary.md"
