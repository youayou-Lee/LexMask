---
title: 端到端评测：v2.1.0-9a9d5fa
date: 2026-11-05
tags:
  - 评测报告
  - issue-37
  - e2e
env: local-nvidia-neo001
target: v2.1.0-9a9d5fa
git: 9a9d5fa
---

# 文档脱敏系统评测报告 — v2.1.0-9a9d5fa

> [!abstract] 一句话诊断
> 🔴 发布冻结：数字保真预算消耗 47.6%（红线 0%）
> 数字保真预算消耗 ==47.6%==（22/42 全对，其中 near_miss 0、真错 20）
> 实体 F1 0.816（无基线对比）
> 实体召回：每 4 个敏感实体漏 1 个（召回 76.9%）
> 延迟 p95 2.6s（承诺线 15s）｜吞吐 28.7 页/分钟

## 0. 健康度速览（给管理者）

> [!danger] 红线指标：数字保真率 —— 预算消耗 47.6%（目标 0%）
> 身份证/电话/银行卡/护照 ==逐字符全对== 是发布红线：错一位 = 该脱的没脱干净。
> 任一错误都计入预算消耗；==消耗必须为 0%== 才可发布。

> [!example]- 四信号（当前 / 目标 / 状态 / 基线对比）
>
> | 信号 | 当前 | 目标 | 状态 | 基线对比 |
> |---|---|---|---|---|
> | 数字保真率 | 52.4% | 100% | 🔴 | — |
> | 实体 F1 | 0.816 | ≥ 0.95（参考） | 🟡 | — |
> | 延迟 p95 | 2.6s | ≤ 15s（承诺线） | 🟢 | — |
> | 吞吐 | 28.7 页/分钟 | 越高越好 | — | — |

## 1. 效果评测

> [!example]- 分类型 P/R/F1（含基线对比）
>
> | 类型 | P | R | F1 | 基线 F1（Δ） |
> |---|---|---|---|---|
> | 公司名称 | 0.0000 | 0.0000 | 0.0000 | — |
> | 地址 | 1.0000 | 1.0000 | 1.0000 | — |
> | 姓名 | 1.0000 | 1.0000 | 1.0000 | — |
> | 开户行 | 0.0000 | 0.0000 | 0.0000 | — |
> | 护照号 | 1.0000 | 1.0000 | 1.0000 | — |
> | 日期 | 1.0000 | 1.0000 | 1.0000 | — |
> | 机关单位 | 0.0000 | 0.0000 | 0.0000 | — |
> | 机构名称 | 0.0000 | 0.0000 | 0.0000 | — |
> | 案号 | 0.0000 | 0.0000 | 0.0000 | — |
> | 电话 | 1.0000 | 1.0000 | 1.0000 | — |
> | 编号 | 0.0000 | 0.0000 | 0.0000 | — |
> | 身份证号 | 1.0000 | 1.0000 | 1.0000 | — |
> | 邮箱 | 1.0000 | 1.0000 | 1.0000 | — |
> | 银行卡号 | 1.0000 | 0.1304 | 0.2308 | — |

> [!note] 指标翻译（不需要背定义）
> **精确率 P**：系统标记为敏感的内容里，多少是真的（「别把无关内容也脱了」）
> **召回率 R**：真正的敏感内容里，系统抓到了多少（「别漏了真正的敏感信息」）
> **near_miss**：数字只差空格/连字符，OCR 噪声，可自动修复；**miss**：真错真丢，要人查

> [!example]- 数字保真率分类型（红线区）
>
> | 类型 | 逐字符正确率 | 目标 | 状态 |
> |---|---|---|---|
> | 身份证号 | 100.0% | 100% | 🟢 |
> | 护照号 | 100.0% | 100% | 🟢 |
> | 电话 | 100.0% | 100% | 🟢 |
> | 银行卡号 | 13.0% | 100% | 🔴 |
> | 邮箱（参考，不入红线） | 100.0% | 越接近 100% 越好 | 🟢 |

## 2. 速度评测

> [!example]- 整体延迟与吞吐
>
> | 指标 | 当前 | 基线 | Δ |
> |---|---|---|---|
> | 单页耗时 p50 | 2.2s | — | — |
> | 单页耗时 p95 | 2.6s | — | — |
> | 吞吐 | 28.7 页/分钟 | — | — |

## 5. 错误明细（工程师下钻）

> [!bug]- 错误列表 48 条（显示前 48，全量见 JSON）——数字噪声 0、数字丢失 20、漏检 28
>
> | 文件 | 页 | 类型 | 错误 | 期望 | 实际 | 疑似阶段 |
> |---|---|---|---|---|---|---|
> | syn_contract_2p_mid | 1 | 机构名称 | 漏检 | 恒达商贸有限公司第9分公司 | — | NER/OCR |
> | syn_contract_2p_mid | 1 | 机构名称 | 漏检 | 恒达建筑工程有限公司第3分公司 | — | NER/OCR |
> | syn_contract_2p_mid | 1 | 机构名称 | 漏检 | 海州区人民法院 | — | NER/OCR |
> | syn_contract_2p_mid | 1 | 机构名称 | 漏检 | 瑞泰实业发展有限公司第8分公司 | — | NER/OCR |
> | syn_judgment_2p_mid | 1 | 机构名称 | 漏检 | 南湖经济技术开发区人民法院 | — | NER/OCR |
> | syn_judgment_2p_mid | 1 | 机构名称 | 漏检 | 海州区人民法院 | — | NER/OCR |
> | syn_contract_txt_1p_mid | 1 | 机构名称 | 漏检 | 恒达商贸有限公司第9分公司 | — | NER/OCR |
> | syn_contract_txt_1p_mid | 1 | 机构名称 | 漏检 | 瑞泰实业发展有限公司第8分公司 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 1028 2084 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 1091 8273 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 1154 6462 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 2035 8105 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 2098 6294 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 2161 4483 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 3042 6126 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 3105 4315 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 4049 4147 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 4112 2336 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 5056 2168 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 5119 8357 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 6063 8189 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 6126 6378 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 7070 6210 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 7133 4399 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 8077 4231 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 8140 2420 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 9084 2252 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 数字丢失 | 6222 0210 9147 8441 | — | OCR/NER |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 1028 2084 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 1091 8273 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 1154 6462 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 2035 8105 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 2098 6294 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 2161 4483 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 3042 6126 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 3105 4315 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 4049 4147 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 4112 2336 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 5056 2168 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 5119 8357 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 6063 8189 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 6126 6378 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 7070 6210 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 7133 4399 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 8077 4231 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 8140 2420 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 9084 2252 | — | NER/OCR |
> | syn_statement_1p_table | 1 | 银行卡号 | 漏检 | 6222 0210 9147 8441 | — | NER/OCR |

> [!failure] 评测失败文件（11）
> syn_contract_1p_mid: RuntimeError: vision 2cf97693-6646-4fed-b65e-e16e75f6b286 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"956b4719f079"}
> syn_contract_10p_mid: RuntimeError: vision 6f3c14b4-b000-4c8c-b84e-e29f4e3ab19c p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"93fc986ab0fa"}
> syn_contract_50p_mid: RuntimeError: vision f66cce0f-64fc-463e-b7e9-6c5c0e3e162d p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"025aacfbab23"}
> syn_judgment_3p_mid: RuntimeError: vision 70504fa6-7a96-44a8-8b18-92d2a1dbdff0 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"239c86adadd6"}
> syn_contract_10p_dense: RuntimeError: vision 8527c80a-38d1-4da2-b40c-cf15d2237ce4 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"f2c135c38d58"}
> syn_contract_10p_sparse: RuntimeError: vision 5138be56-4048-4889-b64f-57861800f8ef p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"e382d73df1a7"}
> syn_contract_3p_mid: RuntimeError: vision 465369b8-b4f7-4bd9-b7ae-b8273f072d8d p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"cf8eb32e60d0"}
> syn_judgment_txt_3p_mid: RuntimeError: vision 3f7f13d4-7df4-48b2-93d2-ba54cbf261a4 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"0869aa275e93"}
> syn_warrant_1p_mid: RuntimeError: vision cfa99008-d6d5-4690-8352-677068224673 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"8ff6343958d0"}
> syn_hybrid_4p_mid: RuntimeError: vision 105170d5-351a-43e4-a055-6fffc52ebc09 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"f989448e74fe"}
> syn_edge_3p_mixed: RuntimeError: vision 7a5f2af7-1e77-4497-913e-95a268b76499 p1 -> HTTP 503: {"error_code":"visual_service_unavailable","message":"视觉检测服务（LocateAnything）未启动：本地请运行 ./start_local.sh --with-visual，或以云端模式启动（--visual cloud，隧道指向自有实例）；若本次任务不需要印章/签字检测，请取消勾选对应视觉识别项。","detail":{},"request_id":"9f15a8358aa3"}

## 6. 结论与行动

> [!danger] 🔴 发布冻结：数字保真预算消耗 47.6%（红线 0%）
> **下一步：**
> - [ ] 排查 银行卡号 丢失 20 例（先看错误明细，定位 OCR 还是识别层）
> - [ ] 排查 11 个评测失败文件（见失败列表）

