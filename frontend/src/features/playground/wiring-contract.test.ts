// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

// Issue#123 门⑥ I-1 / I-2：收益链的**接线层**此前单测零覆盖。
// 评审实测：删掉第一层根因 `setStage('preview')`（自造变异 M15）或删掉 `setVisionStream` 登记（M16），
// `npm test` 照样 157 passed；把 `overlay={overlayModeFor(progressiveRecognition)}` 写死 `'blocking'`（M6）
// 或把 `lockDraw` 里的 `progressiveRecognition` 摘掉（M10）同样全绿。
//
// 本用例是**表达式级契约测试**：把四处接线在源码里钉住。它不替代行为验证——
// 行为面由 `utils.vision-stream-order.test.ts`（数据层顺序）与门④ 浏览器臂（真 DOM 可见性）承担。
// 诚实边界：表达式改名/重构会让本用例变红（这是设计意图：静默丢接线必须被拦住），
// 因此改接线时必须同步改本文件。
// Issue#194 T9：识别页改后台任务流，「文件路径」一组的钉同步换成了任务流接线
// （submitRecognitionTask/watch/进度映射/cancelJob），旧直调（ner/hybrid、runVisionDetectionPages）
// 在 fileHook 里钉为**不得出现**，并在 imageHook 钉为**保留**（AC5 红线）。

// Issue#157：源码读取改走 Vite 的 `?raw` 导入（vitest 原生支持），不再用 `node:fs`/`node:url`。
// 原因：引入 `@types/node` 后全局 `setTimeout` 的重载来自它，三处
// `ReturnType<typeof window.setTimeout>` 的**声明侧**会被 tsc 解析成 `NodeJS.Timeout`，
// 而 `window.setTimeout(...)` 的实参返回仍是 `number` ⇒ 三个产品文件报 TS2322
// （类型门不该改产品类型面）。
// 注意：`eager: true` 是本用例的前提——空集时下面的 `read()` 守卫必须能报到，勿删。
const sources = import.meta.glob(
  './{hooks/use-playground-file.ts,hooks/use-playground-image.ts,hooks/use-playground.ts,playground-page.tsx,playground-context.tsx}',
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>;

const read = (relative: string): string => {
  const content = sources[`./${relative.replace(/^\.\//, '')}`];
  if (typeof content !== 'string') {
    throw new Error(`wiring-contract: 读不到源码 ${relative}（可用键：${Object.keys(sources).join(', ')}）`);
  }
  return content;
};

const fileHook = read('./hooks/use-playground-file.ts');
const imageHook = read('./hooks/use-playground-image.ts');
const playgroundHook = read('./hooks/use-playground.ts');
const page = read('./playground-page.tsx');
const context = read('./playground-context.tsx');

describe('Issue#123 接线契约：四处接线不得被静默摘掉', () => {
  it('第一层根因：多页扫描在识别期即进入 preview（M15 若删掉，这里红）', () => {
    expect(fileHook).toContain("import { shouldEnterPreviewDuringRecognition } from '../lib/progressive-render';");
    // 判定谓词 + 紧邻的 stage 翻转必须成对出现（只留谓词不留 setStage 等于零收益）
    expect(fileHook).toMatch(
      /shouldEnterPreviewDuringRecognition\(\{[\s\S]{0,200}?\}\)\s*\)\s*\{\s*\n\s*setStage\('preview'\);/,
    );
  });

  it('第二层根因：遮罩 overlay 由 overlayModeFor(progressiveRecognition) 决定（M6 若写死 blocking，这里红）', () => {
    expect(page).toContain('overlay={overlayModeFor(progressiveRecognition)}');
    expect(page).toMatch(/import \{[^}]*overlayModeFor[^}]*\} from '\.\/lib\/progressive-render';/);
    expect(page).toContain('lockDraw={replacePageView || progressiveRecognition}');
  });

  it('识别期画布只读：progressiveRecognition 必须参与 lockDraw（M10 若摘掉，这里红）', () => {
    // Issue#194：shouldUsePassiveOverlay 增加第三参 taskDriven——任务驱动的识别一律 passive
    expect(page).toContain(
      'const progressiveRecognition = shouldUsePassiveOverlay(isLoading, visionStream, taskRecognition !== null);',
    );
    // 真进度接线：遮罩面板进度条吃 visionStream（页流式与任务轮询共用管道）
    expect(page).toContain('progress={visionStream}');
  });

  it('Issue#194：文件识别流改后台任务——提交/watch/进度映射/取消的接线钉', () => {
    // 识别页本流程不再直调旧端点/旧函数（AC5：函数与端点保留给重跑等调用方，见下一用例）
    expect(fileHook).not.toContain('runVisionDetectionPages');
    expect(fileHook).not.toContain('ner/hybrid');
    expect(fileHook).toContain("import { submitRecognitionTask } from '@/services/singleTaskApi';");
    expect(fileHook).toMatch(
      /await submitRecognitionTask\(fileId, opts\.latestSelectedTypesRef\.current\);/,
    );
    // provider watch 注册（占位视图即刻可被 taskForFile 命中）
    expect(fileHook).toMatch(/watch\(ref\.job_id, fileId,/);
    // 阶段人话 + 真进度映射（字段换源：任务轮询的 progress_current/total → visionStream）
    expect(fileHook).toContain("setLoadingMessage(t('playground.taskParsing'));");
    expect(fileHook).toContain("t('playground.taskRecognizing')");
    expect(fileHook).toMatch(/setVisionStream\(\{ completed: task\.current, total: task\.total \}\);/);
    // awaiting_review 迁移只执行一次（handledJobRef 守卫）
    expect(fileHook).toMatch(/handledJobRef\.current === task\.jobId\) return;/);
    // 取消走 cancelJob（后端标 cancelled ⇒ provider 报 failed，前端静默）
    expect(fileHook).toContain('void cancelJob(active.jobId).catch(() => {');
  });

  it('重跑路径（图片模式）：同样登记/更新/清空 visionStream', () => {
    expect(imageHook).toMatch(/setVisionStream\?: \(v: \{ completed: number; total: number \} \| null\) => void,/);
    expect(imageHook).toMatch(
      /setVisionStream\?\.\(Math\.max\(1, totalPages\) > 1 \? \{ completed: 0, total: totalPages \} : null\);/,
    );
    expect(imageHook).toMatch(/setVisionStream\?\.\(\{ completed: completedPages, total: totalPages \}\);/);
    expect(imageHook).toMatch(/setVisionStream\?\.\(null\);/);
    // AC5 红线：runVisionDetectionPages 保留（重跑路径仍在用）
    expect(imageHook).toContain('runVisionDetectionPages');
  });

  it('上下文贯通：hook 透传 setVisionStream / visionStream 且进入依赖数组', () => {
    expect(playgroundHook).toMatch(/fileCtx\.setVisionStream,/);
    expect(playgroundHook).toMatch(/visionStream: fileCtx\.visionStream,/);
    expect(context).toContain("visionStream: PlaygroundContextValue['visionStream'];");
    expect(context).toMatch(/visionStream: ctx\.visionStream,/);
    expect(context).toMatch(/ctx\.visionStream,/);
  });

  it('Issue#194：taskRecognition 贯通 hook → context → page，草稿带 taskJobId', () => {
    expect(playgroundHook).toMatch(/taskRecognition: fileCtx\.taskRecognition,/);
    expect(playgroundHook).toMatch(/taskJobId: fileCtx\.taskRecognition\?\.jobId,/);
    expect(playgroundHook).toMatch(/fileCtx\.reattachTask\(/);
    expect(context).toContain("taskRecognition: PlaygroundContextValue['taskRecognition'];");
    expect(context).toMatch(/taskRecognition: ctx\.taskRecognition,/);
    expect(context).toMatch(/ctx\.taskRecognition,/);
  });
});
