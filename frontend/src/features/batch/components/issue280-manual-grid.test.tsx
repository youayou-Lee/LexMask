// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#280 · F1：页格第四态 manual_verified（琥珀格 + 「人工」角标）与详情面板。
//
// 钉住：
//  1) manual 页 data-state="manual_verified"、"人工"角标可见；
//  2) manual 页可进选择集（与 success 同口径，#199 红线只挡 failed）；
//  3) 汇总行含「人工」桶（「成功 N · 人工 M · 失败 K · 未跑 P」）；
//  4) manual 页详情：operator / 补框时间 / 框数 + 撤销按钮（两段确认）；
//  5) auto_completed（自动成功重跑共存）提示行；
//  6) failed 页详情新增「人工补框」按钮（disabled 跟随 retryable 同一准入）。

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

import { BatchPageMatrix } from './batch-page-matrix';

afterEach(() => cleanup());

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-280',
    job_id: 'job-280',
    file_id: 'file-280',
    paged: true,
    page_count: 4,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success', duration_ms: 100, cache_hit: false },
      {
        page: 2,
        state: 'manual_verified',
        cache_hit: false,
        manual: {
          box_count: 3,
          verified_at: '2026-11-15T08:00:00+00:00',
          operator: 'apitester',
          auto_completed: false,
        },
      },
      { page: 3, state: 'failed', reason: 'boom', cache_hit: false },
      { page: 4, state: 'pending' },
    ],
    failed_pages: [3],
    manual_verified_pages: [
      { page: 2, box_count: 3, verified_at: '2026-11-15T08:00:00+00:00', operator: 'apitester' },
    ],
    route_summary: null,
    ...overrides,
  };
}

describe('页格第四态 manual_verified（Issue#280）', () => {
  it('manual 页：data-state=manual_verified、「人工」角标可见、点击进选择集', () => {
    const onChange = vi.fn();
    render(
      <BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={onChange} />,
    );

    const cell = screen.getByTestId('page-cell-2');
    expect(cell.getAttribute('data-state')).toBe('manual_verified');
    expect(screen.getByTestId('page-badge-manual-2')).toBeTruthy();
    // 角标对读屏可访问
    expect(cell.getAttribute('aria-label')).toContain('人工');

    // 与 success 同口径：点击进选择集
    fireEvent.click(cell);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toEqual(new Set([2]));
  });

  it('汇总行含人工桶：成功 1 · 人工 1 · 失败 1 · 未跑 1', () => {
    const onChange = vi.fn();
    render(
      <BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={onChange} />,
    );
    const summary = screen.getByTestId('page-matrix-summary');
    expect(summary.textContent).toContain('成功 1');
    expect(summary.textContent).toContain('人工 1');
    expect(summary.textContent).toContain('失败 1');
    expect(summary.textContent).toContain('未跑 1');
  });

  it('manual 页详情：操作者 / 补框时间 / 框数 + 撤销补框（两段确认）', () => {
    const onChange = vi.fn();
    const onManualRevoke = vi.fn();
    render(
      <BatchPageMatrix
        status={makeStatus()}
        selected={new Set()}
        onChange={onChange}
        onManualRevoke={onManualRevoke}
      />,
    );

    fireEvent.click(screen.getByTestId('page-cell-2'));
    expect(screen.getByTestId('page-detail-manual-meta').textContent).toContain('apitester');
    expect(screen.getByTestId('page-detail-manual-meta').textContent).toContain('3');

    // 第一段：点「撤销补框」出确认（不直接触发撤销）
    fireEvent.click(screen.getByTestId('page-detail-manual-revoke'));
    expect(onManualRevoke).not.toHaveBeenCalled();
    // 第二段：确认后触发
    fireEvent.click(screen.getByTestId('page-detail-manual-revoke-confirm'));
    expect(onManualRevoke).toHaveBeenCalledWith(2);
  });

  it('auto_completed（自动成功重跑共存）详情提示行', () => {
    const onChange = vi.fn();
    render(
      <BatchPageMatrix
        status={makeStatus({
          pages: [
            { page: 1, state: 'success', duration_ms: 100, cache_hit: false },
            {
              page: 2,
              state: 'manual_verified',
              cache_hit: false,
              manual: {
                box_count: 3,
                verified_at: '2026-11-15T08:00:00+00:00',
                operator: 'apitester',
                auto_completed: true,
              },
            },
            { page: 3, state: 'failed', reason: 'boom', cache_hit: false },
            { page: 4, state: 'pending' },
          ],
        })}
        selected={new Set()}
        onChange={onChange}
      />,
    );
    fireEvent.click(screen.getByTestId('page-cell-2'));
    expect(screen.getByTestId('page-detail-manual-coexist')).toBeTruthy();
  });

  it('failed 页详情新增「人工补框」按钮；非审阅态禁用', () => {
    const onManualAnnotate = vi.fn();
    const onChange = vi.fn();
    const { rerender } = render(
      <BatchPageMatrix
        status={makeStatus()}
        selected={new Set()}
        onChange={onChange}
        onManualAnnotate={onManualAnnotate}
      />,
    );
    fireEvent.click(screen.getByTestId('page-cell-3'));
    const button = screen.getByTestId('page-detail-manual-annotate');
    expect(button.getAttribute('disabled')).toBeNull();
    fireEvent.click(button);
    expect(onManualAnnotate).toHaveBeenCalledWith(3);

    // 终态（completed）：disabled 跟随 retryable 同一准入
    rerender(
      <BatchPageMatrix
        status={makeStatus({ item_status: 'completed' })}
        selected={new Set()}
        onChange={onChange}
        onManualAnnotate={onManualAnnotate}
      />,
    );
    const disabled = screen.getByTestId('page-detail-manual-annotate');
    expect(disabled.getAttribute('disabled')).not.toBeNull();
  });

  it('manual_verified_pages 卷级清单随状态透出（normalizer 回归钉）', async () => {
    const { normalizeItemPageStatus } = await import('@/services/jobsApi');
    const raw = {
      item_id: 'item-280',
      job_id: 'job-280',
      file_id: 'file-280',
      paged: true,
      page_count: 2,
      recognition_state: 'done',
      item_status: 'awaiting_review',
      snapshot_at: '2026-11-15T00:00:00+00:00',
      pages: [
        { page: 1, state: 'manual_verified', manual: { box_count: 2, verified_at: 't1', operator: 'op' } },
        { page: 2, state: 'success' },
      ],
      failed_pages: [],
      manual_verified_pages: [{ page: 1, box_count: 2, verified_at: 't1', operator: 'op' }],
    };
    const normalized = normalizeItemPageStatus(raw);
    expect(normalized.manual_verified_pages).toEqual([
      { page: 1, box_count: 2, verified_at: 't1', operator: 'op' },
    ]);
    expect(normalized.pages[0].state).toBe('manual_verified');
    expect(normalized.pages[0].manual?.box_count).toBe(2);
    // 非法 state 回落 pending 不破（#257 契约不回归）
    const fallback = normalizeItemPageStatus({
      ...raw,
      pages: [{ page: 1, state: 'weird' }],
    });
    expect(fallback.pages[0].state).toBe('pending');
  });
});
