# T3 四臂视觉特征检测对比成绩单（Issue#92，v2.0 §1.3 LA 退役前置闸）

- 日期：2026-10-11 · 分支：`feat/issue92-t3-visual-bench` · 评测代码：`eval/benchmarks/t3/`
- 样本：64 页真实案卷扫描页（testdata 快照，43 页含章 / 30 页含签名；难例桶 seal_clean 8 / seal_hard 34 / sig_hard 31）
- GT：框级 174 框（seal 69 / signature 105），方法=四臂并集预标 → 轮 A 确认（4 标注员）→ 轮 B 独立盲标（2 标注员，只看图）→ 差集裁决（83 候选收 18）→ 轮 C 独立抽查（64 框 + 27 疑点加查，通过率 95.31% ≥ 95% 门）。GT 与原始输出落私有区（testdata/benchmarks/t3/、LexMask-workspace/perf-work/issue92-t1/）。
- 判读为预注册（跑批前锁死 eval/benchmarks/t3/spec.py）：塌方=候选臂难例桶召回较 LA 同桶下降 >15pp；误报上界=候选臂 FP/页 ≤2×LA；臂失败页 >10% 无效（本次四臂 64 页全 0 失败，无无效臂）。

## 核心结果（IoU≥0.5，按 kind 分职责判读）

### 印章职责（候选=MinerU seal / PaddleX）

| 臂 | seal 召回 (all) | seal_clean | seal_hard | FP/页 | 单页耗时 |
|---|---|---|---|---|---|
| LA（基线） | 43.5% | 57.1% | 51.0% | 0.08 | 4.48s（GPU） |
| **MinerU seal** | **58.0%** | **71.4%** | **64.7%** | 0.05 | 3.40s（CPU 可） |
| PaddleX seal_recognition | 21.7% | 28.6% | 25.5% | 0.27 | 12.33s（CPU） |

**判读：印章职责可替代——MinerU seal 召回全面 ≥LA（+14.5pp，难例桶无塌方），误报与单页成本均优于 LA，预注册阈值全过。** PaddleX 专用模型两项阈值均未过（召回塌方+误报超界），不采纳。

### 签名职责（候选=conditional-DETR 签名检测器）

| 臂 | signature 召回 (all) | sig_hard | FP/页 | 单页耗时 |
|---|---|---|---|---|
| LA（基线） | 29.5% | 36.0% | 0.52 | （同上，双通道一次） |
| sigdet（CPU） | 25.7% | 32.0% | 2.69 | 100.44s |

**判读：签名职责不可替代——sigdet 召回低 3.8pp 且误报/页为 LA 的 5.2 倍（超 2× 上界）。LA 保留签名职责。**

## 总结论（按预注册二选一）

**LA 不整体退役：印章职责已具备切换条件（MinerU seal 达标），签名职责 LA 留任，差距留档如下：**
1. 签名召回绝对值双双偏低（LA 29.5% / sigdet 25.7%），真实卷面潦草签名+指印混叠是共同难点；sigdet 追平需召回 +3.8pp 且误报压到 1/5——触发条件：#51 视觉难例桶扩充后重测，或 sigdet 阈值/tiled 策略调优后重测。
2. LA 签名通道在 T0 探测 184 页曾 0 命中、本评测 29.5%，其稳定性与 prompt/通道配置相关，切换前任何「LA 部分退役」方案都需先解决印章/签名双通道解耦。
3. 印章线后续可另立「印章职责切 MinerU seal」工程专项（遮盖管线接 seal 块 bbox + 回归），本 Issue 不动现役管线。

## 速度/资源

la 4.48s/页（GPU 8090 常驻）· mineru_seal 3.40s/页（16581 sidecar）· paddlex 12.33s/页（CPU 独立 venv）· sigdet 100.44s/页（CPU 28150，tiled 分钟级）。MinerU 兼可 CPU，GPU 让渡给语义层的 v2.0 设想成立。

## 口径与局限

- GT 预标底为四臂并集，各臂召回存在向上偏置；已用独立补标轮（+18 框，占终版 GT 174 的 10.3%）与轮 C 抽查（95.31%）对冲，**绝对值勿直接外推，臂间相对比较有参考性**。
- MinerU/PaddleX 坐标系经云端对拍实证（0-1000 归一化 / 页像素），见 adapters.py 注释（T2.1）。
- 章内文字识别质量不在本 Issue 范围（NER/T1 线）。

## 产物索引

- 评测代码：`eval/benchmarks/t3/`（spec/adapters/benchmark/report/gt_review + 73 单测）
- 私有区：GT（testdata/benchmarks/t3/gt.jsonl + gt_meta.json + spotcheck.json）、四臂原始输出与叠加图（LexMask-workspace/perf-work/issue92-t1/、云实例私有区，路径见私有执行方案）
- 执行方案+冷评审记录：LexMask-workspace/github-issues/2026-10-11-issue92-visual-bench-plan.md
