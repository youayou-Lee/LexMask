---
title: 协同开发规范 - gh 使用与仓库纪律
tags:
  - process
  - git
aliases:
  - gh工作流
---

> [!tip] 相关文档
> [[WORKFLOW]] · [[开发门禁-测试与独立评审规范]]

# 协同开发规范 — gh 使用与仓库纪律

> 更新日期：2026-09-28。LexMask 为独立产品仓（`youayou-Lee/LexMask`，公开）；上游项目仅作参考实现，不建立同步关系。

## 1. 仓库关系

| 名称 | 仓库 | 说明 |
|---|---|---|
| 产品主仓（origin） | `youayou-Lee/LexMask` | **所有 Issue、开发、PR 都在这里** |
| 参考实现（只读） | `ttttccxxui/DataInfra-RedactionEverything` | 不提 Issue / PR / push，不建 remote 同步 |
| 历史归档（私有） | `youayou-Lee/DataInfra-RedactionEverything-archived` | 开发期历史 Issue 与提交记录，仅回溯用 |

⚠️ 所有 `gh issue / gh pr / gh repo` 命令**显式指定 `--repo youayou-Lee/LexMask`**，防止 gh 按上下文误选其他仓库。

## 2. 与参考实现的关系

> 命名豁免（Issue #1 验收标准 1 的白名单）：上表两个外部仓在 GitHub 的真实仓名含 "DataInfra-RedactionEverything" 字样，指称它们时**必须用真实仓名**（否则死链），不计入旧名残留。



- 仅在 README 致谢中注明；不 fetch、不 merge、不贡献。
- 借鉴其修复/功能时在 Issue/PR 描述中注明来源。

## 3. Issue 工作流

1. **提 Issue**：需求/缺陷先提 Issue（标题带 `[Bug]` / `[Feature]` / `[子任务X]` 前缀，大需求拆 1 个总述 + N 个子任务，互相引用编号）；
2. **方案文档**：正式方案沉淀到仓库 `docs/issue-<编号>-<主题>.md`（结构：背景/分析/方案/验收标准），并在 Issue 正文链接；
3. **复现数据**：涉及真实案卷等敏感数据的文件**一律不入库**，统一放私有数据仓（容器 `testdata/`），文档内用相对路径引用并标注「不入库」；
4. **开发 → 测试 → 验收（AI 自验收或人工，分级见门禁 §4）→ 独立 review → 用户放行 → merge（合入 preview）**：完整门禁见 [开发门禁-测试与独立评审规范.md](./开发门禁-测试与独立评审规范.md)。

## 4. 常用命令速查

```bash
R=youayou-Lee/LexMask

gh issue list   --repo $R
gh issue create --repo $R --title "[Bug] ..." --body-file xxx.md
gh issue close  --repo $R <编号>

gh pr create --repo $R --base main --head feat/xxx
gh pr view / gh pr merge --repo $R <编号>
```

## 5. 归档仓编号引用约定（Issue #59，2026-10-16 定稿）

旧仓 `DataInfra-RedactionEverything` 已于 2026-10-06 弃用冻结（见 `docs/memory/2026-10-06-old-repo-retired.md`），但 v1.0.0 整体迁移把大批旧仓编号带进了本仓文件，而**两仓编号空间碰撞**（如 `#37`/`#40`/`#46` 在仓各有所指）。约定：

1. **外链必须指向本仓真实 Issue**：任何**自动生成或对外可点**的 Issue 引用，都必须指本仓对应编号；**禁止**把旧仓编号拼成 `github.com/youayou-Lee/LexMask/issues/<旧编号>` 的错链（本仓同号 Issue 语义完全不同，读者会被误导）。
2. **指旧仓就写「归档仓 #NN」**并给底稿/文档指针（目标形态示例：`docs/issue-46-format-matrix-testing.md`、`eval/reports/2026-09-15-format-matrix.md` 的「旧仓时期产物，故正文标题保留原 Issue 号」注记）。
3. **历史叙事不批量清理**：代码注释、方案文档、评测底稿里作为历史记录的裸编号**保留原样**（批量改写会擦掉可考据的迁移痕迹，且收益极低）；顺手改动这些文件时可加「归档仓」注记，但**不是义务**。
4. **已识别错链与处置**：`eval/scripts/format_report.py`（render_md 曾拼本仓 #46 错链，Issue #59 修复，并由 `backend/tests/test_eval46_format_matrix.py::test_render_md_snapshot` 钉住）；其余 sweep（`eval/scripts/*`、`eval/datasets/generators/*`、`frontend/src/features/playground/*`、`backend/{app,tests}` 等地约 50+ 处裸编号）按第 3 条**不清理**。
