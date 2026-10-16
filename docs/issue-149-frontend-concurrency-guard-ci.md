# Issue#149 — 前端页并发回归用例进 CI

## 背景

Issue#106（PR#113，`bdc1f23`）解开「勾选视觉特征 ⇒ 页串行」时的判别判据是一条**前端**用例：

- `frontend/src/features/playground/utils.vision-page-concurrency.test.ts`（5 例）
- 判别性断言在 `:97`「勾选印章+签名、不传 concurrency 时，在飞 vision POST 峰值为 3」
  （文件头注释明确：只驱动后端 API 的并发试验不构成判别证据）

但 `.github/workflows/ci.yml` 的 `Frontend build` 作业只有 `npm ci` + `npm run build`，**不跑任何前端测试**（全仓 `vitest` 零调用）。后果：

- 把 `frontend/src/constants/timing.ts:57` 的 `PLAYGROUND_VISION_PAGE_CONCURRENCY` 改回 `1`，
  或把 `frontend/src/features/playground/utils.ts` 的 `effectiveConcurrency` 改回
  `visualFeatureTypes.length ? 1 : concurrency`，**CI 会全绿**；
- 生产端（云实例 10800 = `vite preview` 服务的 `frontend/dist`）随之静默退回串行，
  #106 实测的 2.15–2.47× 收益丢失，用户只能感觉「变慢了」；
- 该常量是唯一开关（低显存部署才置 1），无运行时开关、仓库无 `.git/hooks/pre-commit` ⇒ 无第二道闸。

## 方案

在 `Frontend build` 作业 `npm ci` 之后、`npm run build` 之前插入一步（fail-fast）：

```yaml
      - name: Frontend page-concurrency guard (Issue#106)
        working-directory: frontend
        run: npx vitest run src/features/playground/utils.vision-page-concurrency.test.ts
```

不引入 `npm test` 全量套件（避免时长与 flaky；AC5）。

## 验收标准与证据

| 编号 | 判据 | 证据 |
|---|---|---|
| AC1 | 随既有 `Frontend build` 作业在 `pull_request` / `push`(preview) 触发 | 步骤插在该作业内；YAML `yaml.safe_load` 校验通过，steps 顺序 = install → guard → build |
| AC2-a | 常量改 `1` ⇒ 该步骤**失败** | 本地副本改写 `timing.ts:57` 后跑该用例：**3 failed / 2 passed**（含 `:97` 峰值=3 那条）；还原后 5 passed |
| AC2-b | 重新引入「勾选视觉特征 ⇒ 串行」⇒ 该步骤**失败** | 本地副本把 `utils.ts:321` 改回 `visualFeatureTypes.length ? 1 : concurrency`：**3 failed / 2 passed**；还原后 5 passed |
| AC3 | 正常树通过，增时 ≤ 5s | 本地 `npx vitest run …`：**5 passed，Duration 448–535ms**；实例同款跑法 5 passed 885ms |
| AC4 | 日志可见峰值=3 用例 | 5 例名称含「勾选印章+签名、不传 concurrency 时，在飞 vision POST 峰值为 3」 |
| AC5 | 不跑全量前端套件 | 只指定单文件路径 |

## 边界

- 只加 CI 步骤，不改任何产品代码；低显存部署仍可显式把常量置 1（此时该 CI 步骤会红 —— 属于**有意**的显式变更，需连同用例一并调整，不再可能「静默」退化）。
- 生产端当前状态（2026-10-16 复核）：云实例主树 `85bf9c98`，前端已按该树重新构建（`frontend/dist` 19:07 构建，`vite preview` 10800 已服务新产物 `index-CR-xDua4.js`）；`timing.ts:57 = 3`；两个生产调用点均不传 `concurrency`（`use-playground-file.ts:399`、`use-playground-image.ts:493`）。