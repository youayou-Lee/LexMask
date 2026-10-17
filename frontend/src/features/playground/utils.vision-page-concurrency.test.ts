// Copyright 2026 LexMask Contributors

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { PLAYGROUND_VISION_PAGE_CONCURRENCY } from '@/constants/timing';

// Issue#106 验收 D2（请求级白盒）：页间「同时在飞的 vision POST」峰值。
//
// 本条的判别链条必须经过被改动的 `frontend/src/features/playground/utils.ts`
// （`runVisionDetectionPages`），否则「改动根本没做」也会全绿 —— 只驱动后端
// API 的并发试验属于 P 组前置容量，不构成判别证据。

const inFlight = { current: 0, peak: 0 };
let waiters: Array<() => void> = [];
/** Issue#106 ⑥ M4：记录每次 vision 请求的 URL 与 body，用于断言「每页恰一次 + 视觉特征确实转发」。 */
let calls: Array<{ url: string; body: Record<string, unknown> }> = [];

vi.mock('@/services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api-client')>();
  return {
    ...actual,
    // 部分 mock：必须保留 VISION_TIMEOUT 等其余导出，整模块替换会让
    // `setTimeout(..., undefined)` 立即触发 ⇒ 全部 abort ⇒ 假 FAIL。
    authFetch: vi.fn((url: string, init?: RequestInit) => {
      inFlight.current += 1;
      inFlight.peak = Math.max(inFlight.peak, inFlight.current);
      calls.push({
        url: String(url),
        body: init?.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {},
      });
      return new Promise<Response>((resolve) => {
        waiters.push(() => {
          inFlight.current -= 1;
          resolve({
            ok: true,
            json: async () => ({ bounding_boxes: [] }),
          } as unknown as Response);
        });
      });
    }),
  };
});

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

/** 反复放行当前在飞的请求，直到连续三轮都没有新请求产生。 */
async function drain() {
  let idleStreak = 0;
  for (let guard = 0; guard < 300; guard += 1) {
    await flush();
    if (waiters.length === 0) {
      idleStreak += 1;
      if (idleStreak >= 3) return;
      continue;
    }
    idleStreak = 0;
    const batch = waiters;
    waiters = [];
    for (const release of batch) release();
  }
  throw new Error('drain 未收敛');
}

async function measurePeak(options: {
  totalPages: number;
  visualFeatureTypes?: string[];
  concurrency?: number;
}) {
  inFlight.current = 0;
  inFlight.peak = 0;
  waiters = [];
  calls = [];
  const { runVisionDetectionPages } = await import('./utils');
  const runner = runVisionDetectionPages({
    fileId: 'canary-issue106',
    ocrHasTypes: [],
    label: '测试',
    ...options,
  });
  await drain();
  await runner;
  return inFlight.peak;
}

beforeEach(() => {
  // vitest environment 是 'node'（vite.config.ts:67-73，未装 jsdom）
  // ⇒ utils.ts 用到的 window.setTimeout/clearTimeout 必须自备。
  vi.stubGlobal('window', {
    setTimeout: globalThis.setTimeout,
    clearTimeout: globalThis.clearTimeout,
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('Issue#106 D2：勾选视觉特征后页间不再被强制串行', () => {
  it('勾选印章+签名、不传 concurrency 时，在飞 vision POST 峰值 = 默认页并发（Issue#172 起为 8）', async () => {
    const peak = await measurePeak({
      totalPages: 10,
      visualFeatureTypes: ['official_seal', 'signature'],
    });
    expect(peak).toBe(PLAYGROUND_VISION_PAGE_CONCURRENCY);
  });

  it('显式传 concurrency: 2 生效（不把默认并发写死）', async () => {
    const peak = await measurePeak({
      totalPages: 5,
      visualFeatureTypes: ['official_seal', 'signature'],
      concurrency: 2,
    });
    expect(peak).toBe(2);
  });

  it('未勾视觉特征时同样吃到默认页并发（防回归，非判别证据）', async () => {
    const peak = await measurePeak({ totalPages: 10, visualFeatureTypes: [] });
    expect(peak).toBe(PLAYGROUND_VISION_PAGE_CONCURRENCY);
  });

  it('页数退化：1 页 → 1，2 页 → 2', async () => {
    // 第一段（1 页）在两种分支下都通过，属防回归；真正的判别点在第二段（2 页）。
    expect(
      await measurePeak({ totalPages: 1, visualFeatureTypes: ['official_seal'] }),
    ).toBe(1);
    expect(
      await measurePeak({ totalPages: 2, visualFeatureTypes: ['official_seal'] }),
    ).toBe(2);
  });

  it('每页恰好请求一次，且 selected_visual_feature_types 原样转发（Issue#106 ⑥ M4）', async () => {
    await measurePeak({
      totalPages: 3,
      visualFeatureTypes: ['official_seal', 'signature'],
    });
    expect(calls).toHaveLength(3);
    const pagesHit = calls.map((call) => new URL(call.url, 'http://localhost').searchParams.get('page'));
    expect(new Set(pagesHit)).toEqual(new Set(['1', '2', '3']));
    for (const call of calls) {
      // 若将来把 visualFeatureTypes 漏传，上面 4 条的峰值断言仍会全绿 —— 故在此钉死请求体。
      expect(call.body.selected_visual_feature_types).toEqual(['official_seal', 'signature']);
      expect(call.body.selected_ocr_has_types).toEqual([]);
    }
  });
});

describe('Issue#175 首屏优先：第 1 页独占首发', () => {
  const pageOf = (call: { url: string }) =>
    Number(new URL(call.url, 'http://localhost').searchParams.get('page'));

  async function startRunner(over: { totalPages: number } & Record<string, unknown>) {
    inFlight.current = 0;
    inFlight.peak = 0;
    waiters = [];
    calls = [];
    const { runVisionDetectionPages } = await import('./utils');
    // 注意：必须包在对象里返回 —— async 函数会 await 直接返回的 promise，
    // 那样调用方在放行请求前就永远拿不到控制权（曾因此把两条用例跑成 5s 超时）。
    return {
      runner: runVisionDetectionPages({
        fileId: 'canary-issue175',
        ocrHasTypes: [],
        label: '测试',
        ...over,
      }),
    };
  }

  it('卷页数 > 并发槽位时，首发窗口里只有第 1 页一条在飞（判别点）', async () => {
    const { runner } = await startRunner({ totalPages: 10, visualFeatureTypes: ['official_seal'] });
    // 首发窗口：不给任何请求放行，只让 microtask/宏任务推进几轮
    for (let i = 0; i < 5; i += 1) await flush();
    expect(calls.map(pageOf)).toEqual([1]);
    expect(inFlight.current).toBe(1);

    await drain();
    await runner;
    // 第 1 页之后其余页仍按 k 并发吃满
    expect(calls.map(pageOf).sort((a, b) => a - b)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(inFlight.peak).toBe(PLAYGROUND_VISION_PAGE_CONCURRENCY);
  });

  it('卷页数 ≤ 并发槽位时不启用独占（保持 #106 的峰值契约）', async () => {
    const peak = await measurePeak({
      totalPages: 8,
      visualFeatureTypes: ['official_seal'],
      concurrency: 8,
    });
    expect(peak).toBe(8);
  });

  it('显式关闭 firstPagePriority 退回纯 k 并发（开关本身有判据）', async () => {
    const { runner } = await startRunner({
      totalPages: 10,
      visualFeatureTypes: ['official_seal'],
      firstPagePriority: false,
    });
    for (let i = 0; i < 5; i += 1) await flush();
    expect(calls.length).toBe(PLAYGROUND_VISION_PAGE_CONCURRENCY); // 无独占窗口，直接吃满
    await drain();
    await runner;
  });
});
