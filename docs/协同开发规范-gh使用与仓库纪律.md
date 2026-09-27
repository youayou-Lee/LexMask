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

- 仅在 README 致谢中注明；不 fetch、不 merge、不贡献。
- 借鉴其修复/功能时在 Issue/PR 描述中注明来源。

## 3. Issue 工作流

1. **提 Issue**：需求/缺陷先提 Issue（标题带 `[Bug]` / `[Feature]` / `[子任务X]` 前缀，大需求拆 1 个总述 + N 个子任务，互相引用编号）；
2. **方案文档**：正式方案沉淀到仓库 `docs/issue-<编号>-<主题>.md`（结构：背景/分析/方案/验收标准），并在 Issue 正文链接；
3. **复现数据**：涉及真实案卷等敏感数据的文件**一律不入库**，统一放私有数据仓（容器 `testdata/`），文档内用相对路径引用并标注「不入库」；
4. **开发 → 测试 → PR → 独立 review → 用户验收 → merge（合入 main）**：完整门禁见 [开发门禁-测试与独立评审规范.md](./开发门禁-测试与独立评审规范.md)。

## 4. 常用命令速查

```bash
R=youayou-Lee/LexMask

gh issue list   --repo $R
gh issue create --repo $R --title "[Bug] ..." --body-file xxx.md
gh issue close  --repo $R <编号>

gh pr create --repo $R --base main --head feat/xxx
gh pr view / gh pr merge --repo $R <编号>
```
