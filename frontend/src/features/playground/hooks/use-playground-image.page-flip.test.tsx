// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#207：预览翻页 abort + 加载态（AC1/AC2/AC3/AC10 的 hook 级判据）。
//
// 场景基座：扫描件 PDF（is_scanned=true）走 imageUrl 逐页 effect——即用户
// 反馈「快速连点上下页内容跟不上」的主路径。文本分页路径 B 的内容一致性
// 一半在共享页状态力学（本文件 RapidSettle），一半在 ④ 云冒烟真文本卷
// 翻页（方案 §3 AC10 注记）。
//
// 判别性锚点：
// - AC1 改前（无 abort）：翻页后在飞 signal.aborted 恒 false ⇒ 红；
// - AC2 改前（无 loading 态）：previewPageLoading 不存在 ⇒ 红；
// - AC3 改后仍可证伪：晚到来源是「abort 竞窗内已在途的响应」与「预取晚到」，
//   不依赖被 abort 消灭的「慢响应后到」场景（①.5 r1 意见）。

import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authFetch } from '@/services/api-client';

import type { FileInfo } from '../types';
import { usePlaygroundImage } from './use-playground-image';

vi.mock('@/services/api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api-client')>();
  return { ...actual, authFetch: vi.fn() };
});

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason: unknown) => void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

interface FetchCall {
  url: string;
  signal: AbortSignal | null;
  deferred: Deferred<Response>;
}

let calls: FetchCall[];

// 模块级单例：fileInfo 是 image effect 的依赖，测试内若每渲染新建对象身份，
// effect 会随每次渲染重跑（生产中 fileInfo 是 state、身份稳定）——失败路径
// 会因此出现「loading 永真」的夹具伪象。
const SCAN_FILE: FileInfo = {
  file_id: 'f1',
  filename: 'case.pdf',
  file_size: 100,
  file_type: 'pdf',
  is_scanned: true,
  page_count: 5,
};

function pageResponse(page: number) {
  return new Response(JSON.stringify({ image_base64: `data:image/png;base64,P${page}` }));
}

// 默认取最后一发：同一页可能有「上一代被 abort 的预取/请求」残留（mock 不自动
// reject），当前代的新请求总是最后入列。需要点名叫第 n 发时显式传 nth。
function callForPage(page: number, nth = -1): FetchCall | undefined {
  const list = calls.filter((c) => c.url.includes(`page=${page}`));
  if (list.length === 0) return undefined;
  return nth === -1 ? list[list.length - 1] : list[nth];
}

async function flushMicrotasks() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function resolvedCount(): number {
  return calls.filter((c) => c.url.includes('/preview-image')).length;
}

beforeEach(() => {
  calls = [];
  vi.mocked(authFetch).mockImplementation((input, init) => {
    const d = deferred<Response>();
    // 对齐真实 fetch：abort 即 reject（否则 StrictMode 双跑产生的上一代请求
    // 会永挂 pending，污染时序——真实浏览器不存在这种僵尸请求）
    const signal = init?.signal ?? null;
    const onAbort = () => d.reject(new DOMException('Aborted', 'AbortError'));
    if (signal) {
      if (signal.aborted) onAbort();
      else signal.addEventListener('abort', onAbort, { once: true });
    }
    calls.push({ url: String(input), signal, deferred: d });
    return d.promise;
  });
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('Issue#207 翻页 abort（AC1）', () => {
  it('快速连点 1→2→3：被跳过页的在飞请求被 abort，只采纳落点页', async () => {
    const { result, unmount } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));

    // 首页自动加载
    await waitFor(() => expect(callForPage(1)).toBeDefined());

    await act(async () => {
      result.current.setCurrentPage(2);
    });
    await waitFor(() => expect(callForPage(2)).toBeDefined());
    expect(callForPage(1)!.signal?.aborted).toBe(true);

    await act(async () => {
      result.current.setCurrentPage(3);
    });
    await waitFor(() => expect(callForPage(3)).toBeDefined());
    expect(callForPage(2)!.signal?.aborted).toBe(true);
    expect(callForPage(3)!.signal?.aborted).toBe(false);

    callForPage(3)!.deferred.resolve(pageResponse(3));
    await waitFor(() =>
      expect(result.current.imageUrl).toBe('data:image/png;base64,P3'),
    );
    unmount();
  });

  it('卸载时在飞请求被 abort', async () => {
    const { result, unmount } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));
    await waitFor(() => expect(callForPage(1)).toBeDefined());
    await act(async () => {
      result.current.setCurrentPage(2);
    });
    await waitFor(() => expect(callForPage(2)).toBeDefined());
    unmount();
    expect(callForPage(2)!.signal?.aborted).toBe(true);
  });

  it('翻页时 redacted 预览请求一并 abort', async () => {
    const { result, unmount } = renderHook(() =>
      usePlaygroundImage({ fileInfo: SCAN_FILE, showRedactedPreview: true, redactionVersion: 1 }),
    );
    await waitFor(() => expect(resolvedCount() >= 2).toBe(true)); // 原图 + redacted 各一发
    await act(async () => {
      result.current.setCurrentPage(2);
    });
    await waitFor(() => expect(callForPage(2, 1)).toBeDefined());
    // 页 1 的全部在飞请求（原图 + redacted）都被掐掉
    for (const c of calls.filter((x) => x.url.includes('page=1'))) {
      expect(c.signal?.aborted).toBe(true);
    }
    unmount();
  });
});

describe('Issue#207 加载态（AC2）', () => {
  it('缓存未命中翻页出现 loading，落定/失败即清除且保留旧图', async () => {
    const { result } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));
    await waitFor(() => expect(callForPage(1)).toBeDefined());
    expect(result.current.previewPageLoading).toBe(true);

    callForPage(1)!.deferred.resolve(pageResponse(1));
    await waitFor(() =>
      expect(result.current.imageUrl).toBe('data:image/png;base64,P1'),
    );
    expect(result.current.previewPageLoading).toBe(false);

    // 未命中翻页：loading 出现
    await act(async () => {
      result.current.setCurrentPage(2);
    });
    await waitFor(() => expect(callForPage(2)).toBeDefined());
    expect(result.current.previewPageLoading).toBe(true);
    callForPage(2)!.deferred.resolve(pageResponse(2));
    await waitFor(() => expect(result.current.imageUrl).toBe('data:image/png;base64,P2'));
    expect(result.current.previewPageLoading).toBe(false);

    // 缓存命中回翻：不出现 loading、不发请求
    const before = resolvedCount();
    await act(async () => {
      result.current.setCurrentPage(1);
    });
    expect(result.current.previewPageLoading).toBe(false);
    expect(result.current.imageUrl).toBe('data:image/png;base64,P1');
    expect(resolvedCount()).toBe(before);

    // 失败：loading 清除、旧图保留
    await act(async () => {
      result.current.setCurrentPage(3);
    });
    await waitFor(() => expect(callForPage(3)).toBeDefined());
    expect(result.current.previewPageLoading).toBe(true);
    callForPage(3)!.deferred.reject(new Error('render failed'));
    await waitFor(() => expect(result.current.previewPageLoading).toBe(false));
    expect(result.current.imageUrl).toBe('data:image/png;base64,P1');
  });
});

describe('Issue#207 晚到响应永不覆盖当前页（AC3，改后仍可证伪）', () => {
  it('abort 竞窗内已在途的响应晚到：不生效', async () => {
    const { result } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));
    await waitFor(() => expect(callForPage(1)).toBeDefined());
    await act(async () => {
      result.current.setCurrentPage(2);
    });
    await waitFor(() => expect(callForPage(2)).toBeDefined());
    await act(async () => {
      result.current.setCurrentPage(3);
    });
    await waitFor(() => expect(callForPage(3)).toBeDefined());

    // 页 2 的响应在翻到页 3 之后才抵达（真实 fetch 会因 abort 拒绝；
    // 这里模拟响应已在线上、abort 竞窗内完成的缝隙）
    callForPage(2)!.deferred.resolve(pageResponse(2));
    expect(result.current.imageUrl).not.toBe('data:image/png;base64,P2');

    callForPage(3)!.deferred.resolve(pageResponse(3));
    await waitFor(() =>
      expect(result.current.imageUrl).toBe('data:image/png;base64,P3'),
    );
  });

  it('预取响应晚于翻页抵达：只进缓存，不改显示', async () => {
    const { result } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));
    await waitFor(() => expect(callForPage(1)).toBeDefined());
    callForPage(1)!.deferred.resolve(pageResponse(1));
    await waitFor(() => expect(result.current.imageUrl).toBe('data:image/png;base64,P1'));

    // 空闲预取相邻页（jsdom 无 requestIdleCallback，走 300ms 兜底）
    await waitFor(() => expect(callForPage(2)).toBeDefined(), { timeout: 2000 });

    await act(async () => {
      result.current.setCurrentPage(3);
    });
    await waitFor(() => expect(callForPage(3)).toBeDefined());
    callForPage(2)!.deferred.resolve(pageResponse(2)); // 预取晚到
    callForPage(3)!.deferred.resolve(pageResponse(3));
    await waitFor(() =>
      expect(result.current.imageUrl).toBe('data:image/png;base64,P3'),
    );
  });
});

describe('Issue#207 回归三件套（AC10）', () => {
  it('落定后邻居预取仍发生并入缓存，下次翻页零加载指示（AC10b）', async () => {
    const { result } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));
    await waitFor(() => expect(callForPage(1)).toBeDefined());
    callForPage(1)!.deferred.resolve(pageResponse(1));
    await waitFor(() => expect(result.current.imageUrl).toBe('data:image/png;base64,P1'));

    // 预取相邻页 2 发生
    await waitFor(() => expect(callForPage(2)).toBeDefined(), { timeout: 2000 });
    callForPage(2)!.deferred.resolve(pageResponse(2));
    await flushMicrotasks(); // 等缓存写入落地再翻页（res.json() 有微任务跳数）

    // 翻到已预取的页 2：零加载指示、即时显示
    await act(async () => {
      result.current.setCurrentPage(2);
    });
    expect(result.current.previewPageLoading).toBe(false);
    await waitFor(() =>
      expect(result.current.imageUrl).toBe('data:image/png;base64,P2'),
    );
  });

  it('共享页状态力学：快速连点后落点页稳定（路径 B 共享状态冒烟，AC10a）', async () => {
    const { result } = renderHook(() => usePlaygroundImage({ fileInfo: SCAN_FILE }));
    await waitFor(() => expect(callForPage(1)).toBeDefined());
    await act(async () => {
      result.current.setCurrentPage(5);
      result.current.setCurrentPage(2);
      result.current.setCurrentPage(4);
    });
    expect(result.current.currentPage).toBe(4);
    await waitFor(() => expect(callForPage(4)).toBeDefined());
    expect(callForPage(4)!.signal?.aborted).toBe(false);
  });
});
