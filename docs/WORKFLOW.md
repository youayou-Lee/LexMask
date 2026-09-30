---
title: WORKFLOW - 开发工作流
tags:
  - process
  - workflow
aliases:
  - 工作流
---

> [!tip] 相关文档
> [[开发门禁-测试与独立评审规范]] · [[部署教程]] 见 docs/deploy/

# 工程开发规范：流程、计划、测试

> 仓库 → `youayou-Lee/LexMask`（单人 + AI 协作；gh 操作仍建议显式 `--repo`，防误操作别的仓库）。
> 测试 → backend pytest（`backend/tests/`）；方案文档 → `docs/issue-<编号>-<主题>.md`。
> 原则：**preview 随时可部署（验收/演示实例跟踪它），晋级 main 即发布、tag 只打 main；每个变更有据可查，一切以测试数据为准**（分支模型见 §7）。
> 配套设施：分支保护（禁 force push / 删除）、Issue 模板、Milestone。

## 0. 计划表三层：在哪、怎么看

| 层 | 载体 | 位置 |
|---|---|---|
| 北极星（月级以上） | 项目定位：本地优先的非结构化数据脱敏工作台，国产 DCU 可运行 | README + 版本收口时对照更新 |
| 版本切分 | Roadmap（本文件 §6）+ GitHub Milestone | 见 §6 |
| 任务级 | Issue（挂 Milestone） | GitHub Issues |

**看进度**：

```bash
gh api repos/youayou-Lee/LexMask/milestones --jq '.[] | "\(.title): \(.open_issues) open / \(.closed_issues) closed, due \(.due_on)"'
gh issue list --milestone "v1.1" --state all
```

## 1. 一个功能的生命周期：七步，每步合格标准

### Step 1 立项 —— 构建 Issue（含 ⓪ 需求澄清）
- [ ] 用户一句话反馈（如"加密 PDF 上传失败"）→ AI 负责扩写成完整 Issue，**不是照抄原话**；构建过程中吸收 ⓪ 需求澄清（结构化提问收敛关键分歧），Issue 定稿 = ⓪ 完成的产物，用户确认后才算立完项
- [ ] "要解决的问题"讲清**具体场景、触发条件、内容与边界**（痛点，不是"实现 X"；不写"PDF 加密上传失败"一句话完事）
- [ ] 验收标准 ≥3 条，每条**可测试**（不写"更好用"，写"`curl /health/services` 返回 ocr/ner/visual-features 全 all_online"）
- [ ] 挂 Milestone；预计 >3 天 → 拆成子 Issue

### Step 2 设计 —— 先方案后代码
- [ ] Issue 下补设计评论：模块划分、接缝（改哪些文件）、测试计划
- [ ] 能用一段话讲清"数据怎么从上传文件流到识别→复核→脱敏→导出"；讲不清 → 回去重想
- [ ] 关键决策列 A/B 备选 + 取舍理由
- [ ] **定验收方案（谁来验收）**：AI 自验收或人工验收，写进设计评论——AI 自验收适用于 AI 能凭浏览器实测 / API 实测 / 页面截图逐条核对、不依赖真实案卷与主观判断的改动（典型：纯 UI 显示/文案/样式、纯接口行为），AI 逐条实测留证后直接进独立 review；涉及真实案卷、业务正确性、主观效果判断或验证环境 AI 触达不了的走人工验收。设计评论未写验收方案 → 按人工验收从严（分级细则见门禁规范 §4）
- [ ] **定影响面与环境计划**：标注改动范围（前端 / 后端 / 模型服务 NER|LA|OCR / 多者）；worktree 全量拉码，但开发环境只起影响面内的服务（纯前端改动不起后端，除非有真实依赖）；凡需起服务先核对端口占用——生产与开发端口段分离，多分支并行时按分支分配端口段，防串台
- [ ] **验收方案独立评审（所有 Issue 一律过此门）**：设计+验收方案成稿后、交给用户点头前，调 `reviewing-acceptance-plan` skill 派冷视角子代理评审（只给 Issue 正文 + 验收方案全文，不给设计方案与 diff）——独立起草预期结果、逐条负样本检验（"bug 不修这条必须失败"，答不出即恒真断言，判无效）、补边界攻击面与部署链覆盖；分歧修订后**设计与验收方案一起交给用户确认**，确认后才开工（细则见门禁规范 §1.5）

### Step 3 开发 —— 分支 + 小步提交
- [ ] `git switch preview && git pull --ff-only` 后切 `feat/xxx` / `fix/xxx` / `perf/xxx` / `docs/xxx`；一分支一 Issue
- [ ] 多功能并行时用 worktree 隔离：每分支一个 `../.worktrees/<分支名>` 检出（约定见容器根 AGENTS.md）
- [ ] **测试红不 commit 不 push**；一个 commit 一件事；message `type: 动机`（feat/fix/docs/refactor/test/chore）
- [ ] 混了就 `git rebase -i` 拆

### Step 4 自测 —— 三层（详见 §3）
- [ ] L1 后端 pytest + 前端 build/test/lint 全绿（与 CI 同套）
- [ ] 涉及上传/识别/脱敏/导出链路 → 端到端验证（本地 compose 栈或 `cloud-deploy/e2e_smoke_test.py`），输出留痕贴进 PR
- [ ] GPU 栈（paddle/vLLM/DTK）改动 → 本地不可测，标注风险并安排云实例验证（门禁④）

### Step 5 PR
- [ ] 描述四要素：动机（`Refs #N`）/ 改动（逐模块一句话）/ 验证（数据）/ 风险与回滚
- [ ] **审核阶段**：调 `requesting-code-review` skill 派发 reviewer 子代理（只给 BASE..HEAD diff + 需求描述，不给会话历史；评审清单含完成性对照验收方案、回归与副作用——其他链路/各部署链/共享模型层、规范符合性）；意见按 `receiving-code-review` 处理：先澄清全部不明项再动手，逐条对照代码库验证，Critical 立即修，Important merge 前修，Minor 记 Issue；reviewer 说错要有依据地反驳
- [ ] **放行与合并（2026-09-30 起预授权制）**：用户验收通过时即预授权——review 结论无 Critical/Important → 直接 squash 合并，不再等二次放行；出 Critical/Important → 修完重走 ③④⑤⑥ 后回来找用户。GPU 栈改动仍须门禁④云实例验证通过后才可合
- [ ] CI 全绿（base-guard 强制校验分支模型：功能 PR 须 base=preview 且含最新 preview）；merge 前自己通读一遍 diff

### Step 6 合并收尾（合入 preview）
- [ ] 只用 squash：`gh pr merge --squash --delete-branch`（PR base=preview；误选 main 会被 base-guard 拒）
- [ ] CHANGELOG 当天有条目；`Closes #N` 仅限"本 PR 完全解决该 Issue"，前置/关联一律 `Refs #N`
- [ ] 核对 Milestone 进度

### Step 7 版本收口 —— 打 tag + 复盘
- [ ] 版本验收实例部署验证通过（GPU 栈变更必须过门禁④）→ 先按 §5 晋级 main（squash PR，**不带 --delete-branch**）→ `git tag vX.Y.Z main && git push --tags`
- [ ] 对照 README"路线图"逐项更新勾选
- [ ] CHANGELOG 版本总结：做了什么、验证数据、下一版本为什么是它

## 2. 计划怎么分

```
北极星（本地优先的非结构化数据脱敏，国产 DCU 可运行）
  → Milestone（版本，2-4 周量级）：v1.0 → v1.1 → v1.2
    → Issue（1-3 天原子任务，一 Issue = 一 PR）
```

**拆 Issue 规则**：
- 一 Issue = 一 PR，预计 1-3 天；估超 → 继续拆
- 每版本先打通最小可验收路径，再补边界加固
- 依赖关系写进 Issue 正文（"依赖 #N"）；父 Issue 用 tasklist 跟踪子 Issue
- Milestone 建立时机：上一版本收口时建下一个

## 3. 三层测试体系

| 层 | 测什么 | 怎么跑 | 何时跑 | 合格标准 |
|---|---|---|---|---|
| **L1 单元** | 后端纯逻辑 + 前端组件/工具函数 | `cd backend && pytest tests/`；`cd frontend && npm test` | 每次 commit 前 + CI 强制 | 全绿；新增公开函数/组件配正常例+边界例 |
| **L2 构建静态** | 前端编译 + lint | `cd frontend && npm run build && npm run lint` | 每 PR | 全绿，不引入新告警 |
| **L3 端到端** | 全链路：上传→识别（OCR/NER/视觉）→复核→脱敏→导出 | 本地 `docker compose --profile gpu` 起栈；云实例 `cloud-deploy/e2e_smoke_test.py` | 涉及编排/模型服务/导出的 PR（门禁④） | 场景全过；真实案卷仅云实例使用且测后清理 |

**三条纪律（铁律）**：
1. 测试红 → 不 push 不 merge
2. 修 bug 先写复现测试再修；修复后测试永久留在回归集
3. "应该没问题"不作数，测试与实测数据说话

## 4. 一票否决速查表

| 环节 | 一票否决项 |
|---|---|
| Issue | 无可测试验收标准 → 不开工 |
| 设计 | 讲不清数据流动 → 重想；验收方案未经独立评审（①.5）→ 不开工 |
| 开发 | 测试红 commit → 打回 |
| PR | CI 不绿 / 描述缺要素 → 不 merge |
| 收尾 | CHANGELOG 缺条目 / Issue 关错 → 补完算完 |
| 版本收口 | 验收实例未部署验证 / 路线图未更新 → 不打 tag |

## 5. 常用命令

```bash
# 分支与提交（临时分支从 preview 切）
git switch preview && git pull --ff-only
git switch -c feat/xxx

# Issue / PR
gh issue create -t "标题" -l enhancement -m "v1.1" -b "正文"
gh pr create --fill-first          # 标题取首个 commit，正文补四要素
gh pr checks                       # CI 状态
gh pr merge --squash --delete-branch   # PR base=preview

# 晋级发布（preview 验收通过后；preview 为常驻分支，晋级**不删它**，故不带 --delete-branch）
gh pr create --base main --head preview -t "promote: preview → main"
gh pr merge --squash

# 测试
cd backend && pytest tests/ -v                             # L1 后端
cd frontend && npm run build && npm test && npm run lint   # L1/L2 前端
# L3 端到端：先 docker compose --profile gpu up -d，再跑 cloud-deploy/e2e_smoke_test.py

# 版本收口（在 main 打 tag）
git tag vX.Y.Z main && git push --tags
```

## 6. Roadmap（初始口径：2026-09-28）

以下主题承接自开发期（历史 Issue 留存在归档仓 `youayou-Lee/DataInfra-RedactionEverything-archived`（真实仓名，豁免条目），私有），在 `youayou-Lee/LexMask` 重建 Issue 后挂 v1.1.0 Milestone：

| 主题 | 建仓 Issue 前缀 | 优先级 |
|---|---|---|
| 端到端性能调优（NER 拆请求 / batch 推理后续） | perf | 高 |
| 扫描件阈值可配置化等 review 缓修 | feat | 中 |
| T2 默认词池编号式 / T3 批量 job 级映射 | feat | 中 |
| 客户部署包与验收流程固化 | chore | 高（首个客户环境前） |

> Roadmap 为快照，活口径以 GitHub Issues 为准。

## 7. 分支模型（2026-09-29 改回 preview 线，用户拍板 Issue #20 方案 B）

```
临时分支（feat|fix|perf|docs，从 preview 切，一分支一 Issue，走完八道门）
   └─ squash 合入 preview（PR base=preview；base-guard 强制须含最新 preview）
preview —— 集成/验收线：验收与演示实例跟踪 preview；云实例共享模型层亦跟踪 preview
   └─ 晋级：squash PR（head=preview，base=main）合入 main
main —— 稳定线：发布只发生在晋级；tag 只打在这里（vX.Y.Z，自 v1.0.0 起）
hotfix/<主题>：从 main 切，走门禁后合回 main，打 patch tag（vX.Y.Z → vX.Y.(Z+1)）
```

- 功能分支一律 base=preview；误选 main 会被 ci.yml base-guard 拒（目标 main 只收 preview 晋级或 hotfix/*）。
- 模型服务（NER/LA/OCR）改动 PR 先行合入 preview，消费它的 backend PR 随后——共享模型层跟 preview 更新，backend 分支开发不依赖环境内再起模型服务（多环境槽位细则见部署文档）。
- **配套（已在 GitHub 设置）**：main 分支保护（禁 force push、禁删除）；base-guard 建议进一步配为 required check（待用户确认）。

> 本文件随流程演进更新；改本文件也走 PR（docs/ 前缀）。
