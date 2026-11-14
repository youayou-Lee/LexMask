// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#276 · 页格详情「本页视觉列」行 + 卷头视觉列缺失琥珀提示。
//
// 钉五件事：
//  1) 详情行五文案（done 带命中数 / done 零命中 / skipped / failed / absent）；
//  2) skipped / failed 行琥珀色（text-amber-600），done / absent 不上色；
//  3) 卷头 visual_summary.skipped/failed > 0 ⇒ page-matrix-visual-hint；
//     无缺失或无键（旧后端）⇒ 不渲染；
//  4) en locale 全键成对渲染（i18n parity 的运行时面）；
//  5) 未知 state fail-null（白名单外不渲染，绝不猜）+ normalize 透传回归钉。

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { initI18n, useI18n } from '@/i18n';
import { normalizeItemPageStatus } from '@/services/jobsApi';
import type { ItemPageStatus, ItemPageVisual } from '@/services/jobsApi';

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

import { BatchPageMatrix } from './batch-page-matrix';

afterEach(() => cleanup());

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-276',
    job_id: 'job-276',
    file_id: 'file-276',
    paged: true,
    page_count: 3,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success', duration_ms: 100, cache_hit: false },
      { page: 2, state: 'success', duration_ms: 90, cache_hit: false },
      { page: 3, state: 'success', duration_ms: 80, cache_hit: false },
    ],
    failed_pages: [],
    route_summary: null,
    visual_summary: null,
    ...overrides,
  };
}

function visual(overrides: Partial<ItemPageVisual>): ItemPageVisual {
  return { state: 'done', source: 'la', region_count: 2, zero_hit: false, ...overrides };
}

describe('页详情「本页视觉列」行（Issue#276）', () => {
  it.each([
    [
      'done 带命中数',
      visual({ state: 'done', region_count: 3, zero_hit: false }),
      '本页视觉列：已跑（LA 命中 3 处）',
      'pageDetail.visual',
    ],
    [
      'done 零命中（≠ skipped）',
      visual({ state: 'done', region_count: 0, zero_hit: true }),
      '本页视觉列：已跑（LA 0 命中）',
      'pageDetail.visual',
    ],
    [
      'skipped（LA 不可用）',
      visual({ state: 'skipped', source: 'none', failed_reason: 'la_unavailable' }),
      '本页视觉列：跳过（视觉服务不可用）',
      'pageDetail.visualSkippedLaUnavailable',
    ],
    [
      'failed（视觉阶段异常）',
      visual({ state: 'failed', failed_reason: 'visual_stage_error' }),
      '本页视觉列：失败（视觉阶段异常，文本列不受影响）',
      'pageDetail.visualFailedStage',
    ],
    [
      'absent（未勾选）',
      visual({ state: 'absent', source: 'none' }),
      '本页视觉列：未勾选视觉项',
      'pageDetail.visualAbsent',
    ],
  ])('%s', (_name, v, expected, _key) => {
    useI18n.setState({ locale: 'zh' });
    const status = makeStatus({
      pages: [{ page: 1, state: 'success', duration_ms: 100, cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1, visual: v } }],
      page_count: 1,
    });
    render(<BatchPageMatrix status={status} selected={new Set()} onChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(screen.getByTestId('page-detail-visual').textContent).toBe(expected);
  });

  it('skipped / failed 行琥珀色，done / absent 不上色', () => {
    useI18n.setState({ locale: 'zh' });
    const mk = (v: ItemPageVisual) =>
      makeStatus({
        pages: [
          { page: 1, state: 'success', duration_ms: 100, cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1, visual: v } },
        ],
        page_count: 1,
      });
    const { rerender } = render(
      <BatchPageMatrix status={mk(visual({ state: 'skipped', source: 'none' }))} selected={new Set()} onChange={vi.fn()} />,
    );
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(screen.getByTestId('page-detail-visual').className).toContain('text-amber-600');

    rerender(
      <BatchPageMatrix status={mk(visual({ state: 'failed', failed_reason: 'visual_stage_error' }))} selected={new Set()} onChange={vi.fn()} />,
    );
    expect(screen.getByTestId('page-detail-visual').className).toContain('text-amber-600');

    rerender(
      <BatchPageMatrix status={mk(visual({ state: 'done', region_count: 2 }))} selected={new Set()} onChange={vi.fn()} />,
    );
    expect(screen.getByTestId('page-detail-visual').className).not.toContain('text-amber-600');

    rerender(
      <BatchPageMatrix status={mk(visual({ state: 'absent', source: 'none' }))} selected={new Set()} onChange={vi.fn()} />,
    );
    expect(screen.getByTestId('page-detail-visual').className).not.toContain('text-amber-600');
  });

  it('无 visual 键的旧数据页不渲染该行（省键，不空转崩）', () => {
    useI18n.setState({ locale: 'zh' });
    const status = makeStatus({
      pages: [{ page: 1, state: 'success', duration_ms: 100, cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1 } }],
      page_count: 1,
    });
    render(<BatchPageMatrix status={status} selected={new Set()} onChange={vi.fn()} />);
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(screen.queryByTestId('page-detail-visual')).toBeNull();
  });

  it('en locale：五文案成对渲染（parity 运行时面）', async () => {
    useI18n.setState({ locale: 'en' });
    await initI18n();
    const cases: Array<[ItemPageVisual, string]> = [
      [visual({ state: 'done', region_count: 3 }), 'Visual column：Ran (3 LA hits)'],
      [visual({ state: 'done', region_count: 0, zero_hit: true }), 'Visual column：Ran (0 LA hits)'],
      [visual({ state: 'skipped', source: 'none', failed_reason: 'la_unavailable' }), 'Visual column：Skipped (vision service unavailable)'],
      [visual({ state: 'failed', failed_reason: 'visual_stage_error' }), 'Visual column：Failed (visual stage error; text column unaffected)'],
      [visual({ state: 'absent', source: 'none' }), 'Visual column：No visual types selected'],
    ];
    for (const [v, expected] of cases) {
      cleanup();
      const status = makeStatus({
        pages: [{ page: 1, state: 'success', duration_ms: 100, cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1, visual: v } }],
        page_count: 1,
      });
      render(<BatchPageMatrix status={status} selected={new Set()} onChange={vi.fn()} />);
      fireEvent.click(screen.getByTestId('page-cell-1'));
      expect(screen.getByTestId('page-detail-visual').textContent).toBe(expected);
    }
    useI18n.setState({ locale: 'zh' });
  });
});

describe('卷头视觉列缺失提示（Issue#276）', () => {
  it('skipped/failed > 0 ⇒ 琥珀 hint 行，页号取自 detail.visual', () => {
    useI18n.setState({ locale: 'zh' });
    const status = makeStatus({
      pages: [
        { page: 1, state: 'success', cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1, visual: visual({ state: 'skipped', source: 'none' }) } },
        { page: 2, state: 'success', cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1, visual: visual({ state: 'failed', failed_reason: 'visual_stage_error' }) } },
        { page: 3, state: 'success', cache_hit: false, detail: { route_verdict: 'page_pipeline', attempts: 1, visual: visual({ state: 'done', region_count: 1 }) } },
      ],
      visual_summary: { done: 1, skipped: 1, failed: 1, absent: 0 },
    });
    render(<BatchPageMatrix status={status} selected={new Set()} onChange={vi.fn()} />);
    const hint = screen.getByTestId('page-matrix-visual-hint');
    expect(hint.className).toContain('text-amber-600');
    expect(hint.textContent).toContain('视觉列缺失');
    expect(hint.textContent).toContain('1、2');
    // failed 计数与重试逻辑不受影响
    expect(screen.queryByTestId('page-matrix-retry-failed')).toBeNull();
    expect(screen.getByTestId('page-matrix-summary').textContent).toContain('失败 0');
  });

  it('无缺失（全 done/absent）或旧后端无键 ⇒ 不渲染', () => {
    useI18n.setState({ locale: 'zh' });
    const ok = makeStatus({ visual_summary: { done: 2, skipped: 0, failed: 0, absent: 1 } });
    const { rerender } = render(<BatchPageMatrix status={ok} selected={new Set()} onChange={vi.fn()} />);
    expect(screen.queryByTestId('page-matrix-visual-hint')).toBeNull();

    const legacy = makeStatus();
    delete (legacy as Partial<ItemPageStatus>).visual_summary;
    rerender(<BatchPageMatrix status={legacy} selected={new Set()} onChange={vi.fn()} />);
    expect(screen.queryByTestId('page-matrix-visual-hint')).toBeNull();
  });
});

describe('normalizeItemPageStatus 透传 visual（r2 F7 吞字段回归钉）', () => {
  it('detail.visual 与 visual_summary 逐项保留', () => {
    const normalized = normalizeItemPageStatus({
      item_id: 'item-1',
      job_id: 'job-1',
      file_id: 'file-1',
      paged: true,
      page_count: 1,
      recognition_state: 'done',
      item_status: 'awaiting_review',
      snapshot_at: '2026-11-15T00:00:00+00:00',
      pages: [
        {
          page: 1,
          state: 'success',
          reason: null,
          duration_ms: 100,
          cache_hit: false,
          detail: {
            route_verdict: 'page_pipeline',
            fallback_reason_code: null,
            coverage: null,
            stage_ms: null,
            error_type: null,
            attempts: 1,
            duration_ms: 100,
            visual: { state: 'done', source: 'la', region_count: 2, zero_hit: false },
          },
        },
      ],
      failed_pages: [],
      route_summary: null,
      visual_summary: { done: 1, skipped: 0, failed: 0, absent: 0 },
    });
    expect(normalized.pages[0].detail?.visual).toEqual({
      state: 'done',
      source: 'la',
      region_count: 2,
      zero_hit: false,
    });
    expect(normalized.visual_summary).toEqual({ done: 1, skipped: 0, failed: 0, absent: 0 });
  });

  it('旧后端无 visual / visual_summary 时省键回落', () => {
    const normalized = normalizeItemPageStatus({
      item_id: 'item-1',
      job_id: 'job-1',
      file_id: 'file-1',
      paged: true,
      page_count: 1,
      recognition_state: 'done',
      item_status: 'awaiting_review',
      snapshot_at: '2026-11-15T00:00:00+00:00',
      pages: [{ page: 1, state: 'success' }],
      failed_pages: [],
    });
    expect(normalized.pages[0].detail?.visual).toBeUndefined();
    expect(normalized.visual_summary).toBeNull();
  });

  it('未知 state fail-null（白名单外绝不猜）', () => {
    const normalized = normalizeItemPageStatus({
      item_id: 'item-1',
      job_id: 'job-1',
      file_id: 'file-1',
      paged: true,
      page_count: 1,
      recognition_state: 'done',
      item_status: 'awaiting_review',
      snapshot_at: '2026-11-15T00:00:00+00:00',
      pages: [
        {
          page: 1,
          state: 'success',
          detail: {
            route_verdict: 'page_pipeline',
            attempts: 1,
            visual: { state: 'mystery_state', source: 'la' },
          },
        },
      ],
      failed_pages: [],
    });
    expect(normalized.pages[0].detail?.visual).toBeUndefined();
  });
});
