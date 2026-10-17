// Copyright 2026 LexMask Contributors

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { VisionPageCompletePayload } from './utils';

// Issue#123 门⑥ C-3：AC2（渲染顺序 = 完成顺序、无重复无跳页）此前在仓内**没有任何机器判据**
// （评审实测：删掉 `setVisionStream({completed,total})` 仍 157 passed）。
//
// 本文件钉住 AC2 所依赖的数据层契约：逐页完成回调的**顺序**、`completedPages` 计数、
// 以及「回调载荷逐条追加即可构成渲染序列」这一性质（消费侧 `use-playground-file.ts`
// 正是把每条 `pageBoxes` 直接 append 进 `boundingBoxes`）。
//
// 判别性说明（诚实边界）：本用例在基线 `fcac61a` 上**同样通过**——它钉的是 #123 所依赖的
// 既有流式契约，不是 #123 新增的接线；#123 接线层由 wiring-contract.test.ts 与云端浏览器臂覆盖。

type Box = { id: string; page: number; type: string };

const pending = new Map<number, () => void>();

vi.mock('@/services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api-client')>();
  return {
    ...actual,
    authFetch: vi.fn((url: string) => {
      const page = Number(new URL(String(url), 'http://localhost').searchParams.get('page'));
      return new Promise<Response>((resolve) => {
        pending.set(page, () => {
          resolve({
            ok: true,
            json: async () => ({ bounding_boxes: [{ id: `box-p${page}`, page, type: 'seal' } as Box] }),
          } as unknown as Response);
        });
      });
    }),
  };
});

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

function release(page: number) {
  const resolve = pending.get(page);
  if (!resolve) throw new Error(`page ${page} 没有在飞请求`);
  pending.delete(page);
  resolve();
}

async function runStream(totalPages: number, order: number[]) {
  const payloads: VisionPageCompletePayload[] = [];
  const { runVisionDetectionPages } = await import('./utils');
  const runner = runVisionDetectionPages({
    fileId: 'canary-issue106',
    ocrHasTypes: [],
    visualFeatureTypes: [],
    totalPages,
    concurrency: totalPages,
    // Issue#175 起默认启用「第 1 页独占首发」（卷页数 > 2 时），会把首发窗口收成 1 条 ——
    // 本组用例测的是**回调契约**（完成顺序 ⇒ 渲染顺序、计数器、载荷追加语义），与调度策略无关，
    // 因此显式关闭该开关以保留「任意完成顺序」这一被测面；调度面判据在
    // utils.vision-page-concurrency.test.ts（#175 的独占窗口 + #106 的峰值契约）。
    firstPagePriority: false,
    label: '测试',
    onPageComplete: (payload) => payloads.push(payload),
  });
  for (let i = 0; i < totalPages; i += 1) await flush();
  for (const page of order) {
    release(page);
    await flush();
  }
  const merged = await runner;
  return { payloads, merged };
}

beforeEach(() => {
  pending.clear();
  vi.stubGlobal('window', {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Issue#123 AC2：逐页完成顺序即渲染顺序（无重复、无跳页）', () => {
  it('乱序完成（3→1→2）时回调按完成顺序触发，completedPages 连续无跳号', async () => {
    const { payloads } = await runStream(3, [3, 1, 2]);
    expect(payloads.map((p) => p.page)).toEqual([3, 1, 2]);
    expect(payloads.map((p) => p.completedPages)).toEqual([1, 2, 3]);
    expect(payloads.every((p) => p.totalPages === 3)).toBe(true);
    expect(payloads.every((p) => p.pageBoxes.length === 1)).toBe(true);
  });

  it('回调载荷逐条追加 = 渲染序列：每页恰一次，无重复无跳页', async () => {
    const { payloads, merged } = await runStream(4, [2, 4, 1, 3]);
    const rendered = payloads.flatMap((p) => p.pageBoxes.map((box) => box.page));
    // 渲染序列（消费侧 append 后的结果）严格等于完成顺序
    expect(rendered).toEqual([2, 4, 1, 3]);
    // 无重复无跳页：去重后覆盖全部页，且每页只出现一次
    expect(new Set(rendered)).toEqual(new Set([1, 2, 3, 4]));
    expect(rendered).toHaveLength(4);
    // 汇总返回按页号升序（既有契约，勿混用：流式顺序看回调，最终数组看页号）
    expect(merged.boxes.map((box) => box.page)).toEqual([1, 2, 3, 4]);
    expect(merged.totalBoxes).toBe(4);
  });

  it('进度分母恒定：totalPages 不随完成推进而变化（遮罩 0/N → N/N）', async () => {
    const { payloads } = await runStream(5, [5, 4, 3, 2, 1]);
    expect(payloads.map((p) => `${p.completedPages}/${p.totalPages}`)).toEqual([
      '1/5',
      '2/5',
      '3/5',
      '4/5',
      '5/5',
    ]);
  });
});
