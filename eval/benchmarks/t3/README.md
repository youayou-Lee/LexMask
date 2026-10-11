# T3 视觉检测对比 benchmark（四臂框级检测选型，Issue#92）

`eval/benchmarks/t2/`（文本级 NER）的框级姊妹版：对比四条**框级视觉检测臂**在
真实案卷页图上框「公章 seal / 手写签名 signature」的能力。**选型工具，不做闸门**。

## 四臂

| 臂 | 服务 | 坐标系（原始） | kind |
|---|---|---|---|
| `la` | LA `/detect`（`T3_LA_URL`，默认 127.0.0.1:8090） | raw_answer 0-1000 → 页像素（回退：0-1 归一化结构化框） | seal+signature |
| `mineru_seal` | MinerU sidecar（`T3_MINERU_URL`，默认 16581）一页一任务 | content_list seal 块 bbox=0-1000 归一化 ×页宽高（**云端对拍已实证，T2.1 定案**） | seal |
| `paddlex_seal` | 本地 paddlex `seal_recognition` 产线（`T3_PADDLEX_PIPELINE`，实例按名缓存） | 页像素（T4 实证零换算） | seal |
| `sigdet` | sigdet `/detect`（`T3_SIGDET_URL`，默认 28150） | 0-1 归一化 → 页像素 | signature |

统一接口 `run_arm(arm, image_path) -> list[PredBox]`（adapters.py）；页像素、
左上原点；每臂带超时 + 1 次重试；异常上抛，跑批层记该页 failed（失败页 >10%
判臂无效，见 spec 预注册阈值）。

## 统一数据契约

明细 jsonl 与 GT jsonl 同构（docstring 全文见 `spec.py`）：

```json
{"page_id": "<卷名>#p<N>", "arm": "la|mineru_seal|paddlex_seal|sigdet|gt",
 "kind": "seal|signature", "box": {"x":0,"y":0,"w":0,"h":0},
 "confidence": 1.0, "raw": ""}
```

跑批明细额外追加 `status`（ok|failed）/`elapsed_ms`/`error` 评测字段。

## 预注册阈值（spec.py，先于出数锁定）

- `COLLAPSE_DROP_PP = 15`：候选臂桶召回较 **LA 同桶召回**下降 >15pp → 桶塌方（LA 为基线，自身不判）；
- `FALSE_POS_RATIO_CAP = 2.0`：候选臂 FP/页 > 2×LA FP/页（LA=0 时绝对上限 0.5/页）→ 误报上界突破；
- `ARM_FAIL_PAGE_RATIO = 0.10`：失败页 >10% → 臂无效；
- 匹配：IoU ≥ 0.5 贪心（预测按 confidence 降序，GT 命中即占用）。

## 数据目录约定（仓库零数据红线）

- manifest / 页图 / GT 只放私有区：`testdata/benchmarks/t3/`
  （`manifest.private.json` + `pages/<page_id>.png` + `gt.jsonl`），不入主仓；
- 跑批产物（metrics/details/report）默认落 `eval/reports/t3-<ts>/`。

## 快速用法

```bash
# 1. manifest 构建/校验（数据来自 T0 探测原始输出 perf-work/issue92-t0/）
PYTHONPATH=$PWD python3 eval/benchmarks/t3/build_sample.py \
  --probe-dir <perf-work/issue92-t0> --out testdata/benchmarks/t3/manifest.private.json
PYTHONPATH=$PWD python3 eval/benchmarks/t3/build_sample.py --validate testdata/benchmarks/t3/manifest.private.json

# 2. 四臂跑批（--arms la,sigdet --limit 3 冒烟）
#    paddlex 臂注意：export PADDLE_PDX_CACHE_HOME=/root/private_data/redaction-persist/paddlex-cache
#    （独立缓存目录，别写 ~/.paddlex 共享目录——T4 实测避坑）
PYTHONPATH=$PWD python3 eval/benchmarks/t3/benchmark_t3.py \
  --manifest testdata/benchmarks/t3/manifest.private.json \
  --pages-dir testdata/benchmarks/t3/pages

# 3. GT 标注核对页（浏览器打开 → 增删改框 → 导出 gt.jsonl → 校验落盘）
PYTHONPATH=$PWD python3 eval/benchmarks/t3/gt_review.py build \
  --pages-dir testdata/benchmarks/t3/pages \
  --prefill eval/reports/t3-<ts>/details.jsonl --prefill-arm la \
  --out testdata/benchmarks/t3/gt_review.html
PYTHONPATH=$PWD eval/benchmarks/t3/gt_review.py export \
  --downloaded ~/Downloads/gt.jsonl --out testdata/benchmarks/t3/gt.jsonl

# 4. 成绩单（分桶 × 臂 × kind + 接入成本 + 预注册阈值自动判读）
PYTHONPATH=$PWD python3 eval/benchmarks/t3/report_t3.py \
  --metrics eval/reports/t3-<ts>/metrics.json \
  --details eval/reports/t3-<ts>/details.jsonl \
  --gt testdata/benchmarks/t3/gt.jsonl \
  --manifest testdata/benchmarks/t3/manifest.private.json
```

## 单测

```bash
python3 -m pytest eval/benchmarks/t3/tests/ -q
```

零真实服务依赖：HTTP 用线程内 mock server 回放；合成数据；覆盖 IoU/贪心匹配、
坐标换算（0-1000 制 / 0-1 制）、schema 校验、塌方/误报/臂无效判定、适配器
超时→failed 路径。
