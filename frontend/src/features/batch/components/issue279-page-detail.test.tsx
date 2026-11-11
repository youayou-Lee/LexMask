// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#279 Task 8 · 页格展开「这页发生了什么」+ 卷头路由摘要（A4.3：只读既有
// page-status 响应扩展字段，不新增任何 JSONL 读取接口）。
//
// 钉五件事：
//  1) 点页格展开详情面板：路由 / 各阶段耗时 / 总耗时 / 重试次数，数字与
//     pages[i].duration_ms、detail.duration_ms、detail.stage_ms 三源一致；
//  2) 失败页展开显示机读 error_type 与重试次数（attempts = 已重试次数 + 1）；
//  3) 卷头 chip：fallback 码翻人话（watermark_overlay_hit→水印命中…），未知码直显原文；
//  4) docline 卷头 chip 显示「已接管」文案，页级路由显示「文本层链接管」；
//  5) normalizeItemPageStatus 必须保留 detail / route_summary（响应模型吞字段回归钉）。

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { normalizeItemPageStatus } from '@/services/jobsApi';
import type { ItemPageStatus } from '@/services/jobsApi';

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

import { BatchPageMatrix } from './batch-page-matrix';

afterEach(() => cleanup());

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-279',
    job_id: 'job-279',
    file_id: 'file-279',
    paged: true,
    page_count: 4,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-12T00:00:00+00:00',
    pages: [
      {
        page: 1,
        state: 'success',
        duration_ms: 100,
        cache_hit: false,
        detail: {
          route_verdict: 'page_pipeline',
          fallback_reason_code: 'no_text_layer',
          stage_ms: { ocr_has: 90 },
          error_type: null,
          attempts: 1,
          duration_ms: 100,
        },
      },
      {
        page: 2,
        state: 'failed',
        reason: 'ValueError: boom',
        duration_ms: 50,
        cache_hit: false,
        detail: {
          route_verdict: 'page_pipeline',
          fallback_reason_code: 'no_text_layer',
          error_type: 'ValueError',
          attempts: 3,
          duration_ms: 50,
        },
      },
      { page: 3, state: 'success', duration_ms: 80, cache_hit: false },
      { page: 4, state: 'pending' },
    ],
    failed_pages: [2],
    route_summary: null,
    ...overrides,
  };
}

describe('页格展开「这页发生了什么」（Issue#279）', () => {
  it('点页格展开详情：路由 / 分段耗时 / 总耗时 / 重试，三源数字一致，再点收起', () => {
    const onChange = vi.fn();
    render(<BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={onChange} />);

    // 无路由证据的卷不出 chip。
    expect(screen.queryByTestId('page-route-chip')).toBeNull();
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();

    // 点成功页：选择照旧发生（#257 交互不破坏），同时展开详情。
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(onChange).toHaveBeenCalledTimes(1);

    const panel = screen.getByTestId('page-detail-panel');
    expect(panel.getAttribute('data-page')).toBe('1');
    // 路由 = 逐页识别 + fallback 码人话。
    expect(screen.getByTestId('page-detail-route').textContent).toContain('路由');
    expect(screen.getByTestId('page-detail-route').textContent).toContain('逐页识别');
    expect(screen.getByTestId('page-detail-route').textContent).toContain('无内嵌文本层');
    // 分段耗时：ocr_has → OCR 人话。
    expect(screen.getByTestId('page-detail-stage-ms').textContent).toContain('OCR 90ms');
    // 总耗时三源一致：pages[0].duration_ms === detail.duration_ms === 面板展示值。
    const status = makeStatus();
    expect(status.pages[0].duration_ms).toBe(status.pages[0].detail?.duration_ms);
    expect(screen.getByTestId('page-detail-duration').textContent).toContain('总耗时 100ms');
    // 无重试账 → 未重试（attempts=1）。
    expect(screen.getByTestId('page-detail-attempts').textContent).toContain('未重试');

    // 再点同一页：收起。
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('失败页展开显示机读 error_type 与重试次数（attempts=已重试次数+1）', () => {
    const onChange = vi.fn();
    // 不传 onRetryPage：点击红叉页不应触发重试，而是能看到失败详情。
    render(<BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={onChange} />);

    fireEvent.click(screen.getByTestId('page-cell-2'));
    const panel = screen.getByTestId('page-detail-panel');
    expect(panel.getAttribute('data-page')).toBe('2');
    // 机读异常类型（非人话，原样透出，便于对照日志）。
    expect(screen.getByTestId('page-detail-error-type').textContent).toContain('ValueError');
    // 重试账 page_retry_counts=2 → attempts=3。
    expect(screen.getByTestId('page-detail-attempts').textContent).toContain('重试 3 次');
    // 失败页不进选择集（#199 红线不动）。
    expect(onChange).not.toHaveBeenCalled();
  });

  it('无 detail 的旧数据页照常展开（attempts 回落 1，不空面板崩）', () => {
    render(<BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId('page-cell-3'));
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('3');
    expect(screen.getByTestId('page-detail-attempts').textContent).toContain('未重试');
    expect(screen.queryByTestId('page-detail-error-type')).toBeNull();
    expect(screen.queryByTestId('page-detail-stage-ms')).toBeNull();
  });
});

describe('卷头路由摘要 chip（Issue#279）', () => {
  it('fallback 卷：码翻人话，未知码直显原文', () => {
    const { rerender } = render(
      <BatchPageMatrix
        status={makeStatus({
          route_summary: { verdict: 'page_pipeline', fallback_reason_code: 'watermark_overlay_hit' },
        })}
        selected={new Set()}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('page-route-chip').textContent).toBe(
      '文本层链未接管（水印命中）·走逐页识别',
    );

    rerender(
      <BatchPageMatrix
        status={makeStatus({
          route_summary: { verdict: 'page_pipeline', fallback_reason_code: 'mystery_new_code' },
        })}
        selected={new Set()}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('page-route-chip').textContent).toContain('mystery_new_code');
  });

  it('docline 卷：显示「已接管」文案；页级路由显示「文本层链接管」', () => {
    render(
      <BatchPageMatrix
        status={makeStatus({
          route_summary: { verdict: 'docline', fallback_reason_code: null },
          pages: [
            {
              page: 1,
              state: 'success',
              duration_ms: 200,
              cache_hit: false,
              detail: { route_verdict: 'docline', coverage: 0.9, attempts: 1, duration_ms: 200 },
            },
            { page: 2, state: 'pending' },
          ],
        })}
        selected={new Set()}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByTestId('page-route-chip').textContent).toBe('文本层链已接管');

    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(screen.getByTestId('page-detail-route').textContent).toContain('文本层链接管');
  });
});

describe('page-status 响应归一化保留 detail / route_summary（吞字段回归钉）', () => {
  it('detail 与 route_summary 字段逐项保留', () => {
    const normalized = normalizeItemPageStatus({
      item_id: 'item-1',
      job_id: 'job-1',
      file_id: 'file-1',
      paged: true,
      page_count: 1,
      recognition_state: 'done',
      item_status: 'awaiting_review',
      snapshot_at: '2026-11-12T00:00:00+00:00',
      pages: [
        {
          page: 1,
          state: 'success',
          reason: null,
          duration_ms: 100,
          cache_hit: false,
          detail: {
            route_verdict: 'page_pipeline',
            fallback_reason_code: 'no_text_layer',
            coverage: null,
            stage_ms: { ocr_has: 90 },
            error_type: null,
            attempts: 1,
            duration_ms: 100,
          },
        },
      ],
      failed_pages: [],
      route_summary: { verdict: 'page_pipeline', fallback_reason_code: 'no_text_layer' },
    });

    expect(normalized.pages[0].detail).toEqual({
      route_verdict: 'page_pipeline',
      fallback_reason_code: 'no_text_layer',
      coverage: null,
      stage_ms: { ocr_has: 90 },
      error_type: null,
      attempts: 1,
      duration_ms: 100,
    });
    expect(normalized.route_summary).toEqual({
      verdict: 'page_pipeline',
      fallback_reason_code: 'no_text_layer',
    });
  });

  it('旧后端无 detail / route_summary 时不炸（回落 null）', () => {
    const normalized = normalizeItemPageStatus({
      item_id: 'item-1',
      job_id: 'job-1',
      file_id: 'file-1',
      paged: true,
      page_count: 1,
      recognition_state: 'done',
      item_status: 'awaiting_review',
      snapshot_at: '2026-11-12T00:00:00+00:00',
      pages: [{ page: 1, state: 'success' }],
      failed_pages: [],
    });
    expect(normalized.pages[0].detail).toBeNull();
    expect(normalized.route_summary).toBeNull();
  });
});
