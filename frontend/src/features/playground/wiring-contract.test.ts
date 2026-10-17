// Copyright 2026 LexMask Contributors

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

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

const read = (relative: string) =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf-8');

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
    expect(page).toContain('const progressiveRecognition = shouldUsePassiveOverlay(isLoading, visionStream);');
  });

  it('文件路径：逐页检测前登记 {completed: 0}，逐页完成时更新，finally 清空（M16 若删登记，这里红）', () => {
    expect(fileHook).toMatch(
      /setVisionStream\(totalPages > 1 \? \{ completed: 0, total: totalPages \} : null\);/,
    );
    expect(fileHook).toMatch(/setVisionStream\(\{ completed: completedPages, total: totalPages \}\);/);
    expect(fileHook).toMatch(/finally \{[\s\S]{0,400}?setVisionStream\(null\);/);
  });

  it('重跑路径（图片模式）：同样登记/更新/清空 visionStream', () => {
    expect(imageHook).toMatch(/setVisionStream\?: \(v: \{ completed: number; total: number \} \| null\) => void,/);
    expect(imageHook).toMatch(
      /setVisionStream\?\.\(Math\.max\(1, totalPages\) > 1 \? \{ completed: 0, total: totalPages \} : null\);/,
    );
    expect(imageHook).toMatch(/setVisionStream\?\.\(\{ completed: completedPages, total: totalPages \}\);/);
    expect(imageHook).toMatch(/setVisionStream\?\.\(null\);/);
  });

  it('上下文贯通：hook 透传 setVisionStream / visionStream 且进入依赖数组', () => {
    expect(playgroundHook).toMatch(/fileCtx\.setVisionStream,/);
    expect(playgroundHook).toMatch(/visionStream: fileCtx\.visionStream,/);
    expect(context).toContain("visionStream: PlaygroundContextValue['visionStream'];");
    expect(context).toMatch(/visionStream: ctx\.visionStream,/);
    expect(context).toMatch(/ctx\.visionStream,/);
  });
});
