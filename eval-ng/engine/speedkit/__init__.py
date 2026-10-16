"""speedkit —— eval-ng 速度测量模式（Issue #131）的单一产出口。

设计文档：`docs/issue-131-speed-module-design.md`（公开仓）；验收方案在私有仓
`LexMask-workspace/docs/acceptance/2026-10-16-issue131-速度测量模块验收方案.md`。

分层：

- `collect`：两线响应 → 统一页信号 + 聚合 + 闭合/残差/非同源判定（键路径按 `<pipeline>.<key>`）。
- `driver`：`single`（逐页 vision，客户端 perf_counter 计墙钟）/ `batch`（smart_batch 作业 + 轮询）。
- `arms`：臂定义 schema + 工作树干净/还原/patch 生效断言（引擎零 spawn、零改文件）。
- `report`：报告装配 + 白名单/脚注自足校验 + **四态判定**（PASS/FAIL/BLOCKED/UNVERIFIED）。

与既有物的关系：`engine/scorers/speed.py` 的 `score_speed` 是**质量线**的汇总裁（被 `runner`
硬编码调用），speedkit 复用其汇总裁不重复第三套口径；`participants/backend_pipeline.py` 是
`run` 路径的参赛者，与 speedkit 的分层无关。旧 `backend/scripts/eval/benchmark_throughput.py`
保留为旁证，不修改、不 import。
"""

from __future__ import annotations

from . import arms, collect, driver, report

__all__ = ["arms", "collect", "driver", "report"]
