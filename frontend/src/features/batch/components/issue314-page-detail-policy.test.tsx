// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#314 · 页格点击语义：选择与诊断分离。
//
// 钉三档 detailPolicy：
//  1) none（导出场景）：点任何页只切换选择；不渲染「第 N 页发生了什么」详情面板、
//     不渲染卷头 route chip（即使响应里有 route_summary）；失败页仍不可选；
//  2) issues（批量审阅）：成功页点击只切换选择；失败/人工页点击展开详情，
//     重试 / 撤销人工框入口保留且可达；
//  3) 缺省 always：现行为兼容（成功页展开 + 选择）。
// Shift 连选、范围输入、手动页可选等选择行为在三档下都不回归。

import { useState } from 'react';

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

import { BatchPageMatrix } from './batch-page-matrix';

afterEach(() => cleanup());

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-314',
    job_id: 'job-314',
    file_id: 'file-314',
    paged: true,
    page_count: 5,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      {
        page: 1,
        state: 'success',
        detail: {
          route_verdict: 'page_pipeline',
          fallback_reason_code: 'no_text_layer',
          attempts: 1,
        },
      },
      { page: 2, state: 'success' },
      {
        page: 3,
        state: 'failed',
        reason: 'ner boom',
        detail: { route_verdict: 'page_pipeline', error_type: 'NerError', attempts: 2 },
      },
      {
        page: 4,
        state: 'manual_verified',
        manual: {
          box_count: 2,
          verified_at: '2026-11-15T01:00:00+00:00',
          operator: 'u1',
          auto_completed: false,
        },
      },
      { page: 5, state: 'pending' },
    ],
    failed_pages: [3],
    route_summary: { verdict: 'page_pipeline', fallback_reason_code: 'no_text_layer' },
    ...overrides,
  };
}

type Policy = 'always' | 'issues' | 'none';

/** 受控宿主：选择集真实回写，Shift 连选/范围输入才有可观察的连续状态。 */
function ControlledMatrix({
  policy,
  status = makeStatus(),
  onSelectionChange,
  ...rest
}: {
  policy: Policy;
  status?: ItemPageStatus;
  onSelectionChange?: (next: Set<number>) => void;
  onRetryPage?: (page: number) => void;
  onManualAnnotate?: (page: number) => void;
  onManualRevoke?: (page: number) => void;
}) {
  const [selected, setSelected] = useState<Set<number>>(new Set());
  return (
    <BatchPageMatrix
      status={status}
      selected={selected}
      onChange={(next) => {
        onSelectionChange?.(next);
        setSelected(next);
      }}
      detailPolicy={policy}
      {...rest}
    />
  );
}

function selectedPages(): number[] {
  return screen
    .getAllByTestId(/^page-cell-/)
    .filter((cell) => cell.getAttribute('data-selected') === 'true')
    .map((cell) => Number(cell.getAttribute('data-testid')?.replace('page-cell-', '')))
    .sort((a, b) => a - b);
}

describe('detailPolicy=none：导出场景点页只改选择（Issue#314）', () => {
  it('点成功页只切换选择；不渲染详情面板、不渲染 route chip', () => {
    const onSelectionChange = vi.fn();
    render(<ControlledMatrix policy="none" onSelectionChange={onSelectionChange} />);

    expect(screen.getByTestId('page-matrix').getAttribute('data-detail-policy')).toBe('none');
    // route_summary 有值也不出 chip：导出场景不暴露技术诊断。
    expect(screen.queryByTestId('page-route-chip')).toBeNull();

    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
    expect(selectedPages()).toEqual([1]);
    // 「第 N 页发生了什么」整块不存在——不是渲染后隐藏。
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(screen.queryByText(/第 1 页发生了什么/)).toBeNull();

    // 再点同一页 = 取消选择，同样不弹面板。
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(selectedPages()).toEqual([]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('点失败页无选择变化、无详情面板（#199 红线不变，错误状态仍可见）', () => {
    const onSelectionChange = vi.fn();
    render(<ControlledMatrix policy="none" onSelectionChange={onSelectionChange} />);

    const failedCell = screen.getByTestId('page-cell-3');
    // 错误状态可见：格色数据 + title 带失败状态（技术面板才是被藏掉的东西）。
    expect(failedCell.getAttribute('data-state')).toBe('failed');
    expect(failedCell.getAttribute('title')).toContain('失败');
    // 导出场景下不可选格子的光标诚实：点了没有事发生。
    expect(failedCell.className).toContain('cursor-not-allowed');

    fireEvent.click(failedCell);
    expect(onSelectionChange).not.toHaveBeenCalled();
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('点人工页只切换选择，不展开详情（撤销入口不出现）', () => {
    render(<ControlledMatrix policy="none" />);

    fireEvent.click(screen.getByTestId('page-cell-4'));
    expect(selectedPages()).toEqual([4]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(screen.queryByTestId('page-detail-manual-revoke')).toBeNull();
  });

  it('Shift 连选照常：失败页被跳过，人工页可并入', () => {
    render(<ControlledMatrix policy="none" />);

    fireEvent.click(screen.getByTestId('page-cell-1'));
    fireEvent.click(screen.getByTestId('page-cell-4'), { shiftKey: true });
    expect(selectedPages()).toEqual([1, 2, 4]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('范围输入照常整段替换选择，不产生详情面板', () => {
    render(<ControlledMatrix policy="none" />);

    fireEvent.change(screen.getByTestId('page-matrix-range-input'), {
      target: { value: '1-2,4' },
    });
    fireEvent.click(screen.getByTestId('page-matrix-range-apply'));
    expect(selectedPages()).toEqual([1, 2, 4]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('全选 = success ∪ manual_verified，失败/pending 不入选（AC4 显式判据）', () => {
    const onSelectionChange = vi.fn();
    render(<ControlledMatrix policy="none" onSelectionChange={onSelectionChange} />);

    fireEvent.click(screen.getByTestId('page-matrix-select-all'));
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
    expect([...onSelectionChange.mock.calls[0][0]].sort((a, b) => a - b)).toEqual([1, 2, 4]);
    expect(selectedPages()).toEqual([1, 2, 4]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });
});

describe('detailPolicy=issues：批量审阅只有问题页展开（Issue#314）', () => {
  it('成功页点击只切换选择，不展开详情', () => {
    const onSelectionChange = vi.fn();
    render(<ControlledMatrix policy="issues" onSelectionChange={onSelectionChange} />);

    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
    expect(selectedPages()).toEqual([1]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('失败页展开详情且重试按钮可达；不进入选择集', () => {
    const onSelectionChange = vi.fn();
    const onRetryPage = vi.fn();
    render(
      <ControlledMatrix
        policy="issues"
        onSelectionChange={onSelectionChange}
        onRetryPage={onRetryPage}
      />,
    );

    fireEvent.click(screen.getByTestId('page-cell-3'));
    expect(onSelectionChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('3');
    const retry = screen.getByTestId('page-detail-retry');
    expect(retry.hasAttribute('disabled')).toBe(false);
    fireEvent.click(retry);
    expect(onRetryPage).toHaveBeenCalledWith(3);
  });

  it('人工页展开详情并切换选择；撤销入口可达', () => {
    const onSelectionChange = vi.fn();
    const onManualRevoke = vi.fn();
    render(
      <ControlledMatrix
        policy="issues"
        onSelectionChange={onSelectionChange}
        onManualRevoke={onManualRevoke}
      />,
    );

    fireEvent.click(screen.getByTestId('page-cell-4'));
    expect(onSelectionChange).toHaveBeenCalledTimes(1);
    expect(selectedPages()).toEqual([4]);
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('4');
    expect(screen.getByTestId('page-detail-manual-meta')).toBeTruthy();
    expect(screen.getByTestId('page-detail-manual-revoke').hasAttribute('disabled')).toBe(false);
  });

  it('问题页再点一次收起详情（单槽语义保留）', () => {
    render(<ControlledMatrix policy="issues" />);

    fireEvent.click(screen.getByTestId('page-cell-3'));
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('3');
    fireEvent.click(screen.getByTestId('page-cell-3'));
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('route chip 保留：审阅卷头诊断面不丢', () => {
    render(<ControlledMatrix policy="issues" />);
    expect(screen.getByTestId('page-route-chip').textContent).toContain('逐页识别');
  });

  it('pending 页不是「死点击」：无选择、无面板，光标与 aria-disabled 据实', () => {
    const onSelectionChange = vi.fn();
    render(<ControlledMatrix policy="issues" onSelectionChange={onSelectionChange} />);

    const pendingCell = screen.getByTestId('page-cell-5');
    expect(pendingCell.getAttribute('data-state')).toBe('pending');
    // 既不展开（issues 只展开问题页）也选不中（pending 不可导出）⇒ 点击无反馈。
    expect(pendingCell.className).toContain('cursor-not-allowed');
    expect(pendingCell.getAttribute('aria-disabled')).toBe('true');

    fireEvent.click(pendingCell);
    expect(onSelectionChange).not.toHaveBeenCalled();
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('展开失败页后再点成功页：旧面板收起，点击页与面板不残留', () => {
    render(<ControlledMatrix policy="issues" />);

    fireEvent.click(screen.getByTestId('page-cell-3'));
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('3');

    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(selectedPages()).toEqual([1]);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });

  it('失败页在面板内重试成功（page_count 不变）后旧面板消失', () => {
    const failed = makeStatus();
    const retried = makeStatus({
      pages: [
        { page: 1, state: 'success' },
        { page: 2, state: 'success' },
        { page: 3, state: 'success' },
        {
          page: 4,
          state: 'manual_verified',
          manual: {
            box_count: 2,
            verified_at: '2026-11-15T01:00:00+00:00',
            operator: 'u1',
            auto_completed: false,
          },
        },
        { page: 5, state: 'pending' },
      ],
      failed_pages: [],
    });
    const { rerender } = render(<ControlledMatrix policy="issues" status={failed} />);

    fireEvent.click(screen.getByTestId('page-cell-3'));
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('3');

    rerender(<ControlledMatrix policy="issues" status={retried} />);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(screen.getByTestId('page-cell-3').getAttribute('data-expanded')).toBe('false');
  });
});

describe('缺省 always：未迁移宿主兼容现行为（Issue#314）', () => {
  it('不传 detailPolicy 时点成功页仍展开详情并切换选择', () => {
    const onChange = vi.fn();
    render(<BatchPageMatrix status={makeStatus()} selected={new Set()} onChange={onChange} />);

    expect(screen.getByTestId('page-matrix').getAttribute('data-detail-policy')).toBe('always');
    fireEvent.click(screen.getByTestId('page-cell-1'));
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('1');
    expect(screen.getByTestId('page-route-chip')).toBeTruthy();
  });

  it('always + disabled：只读矩阵不展开详情，光标与 aria 不撒谎（Minor-2）', () => {
    render(
      <BatchPageMatrix
        status={makeStatus()}
        selected={new Set()}
        onChange={vi.fn()}
        disabled
      />,
    );

    const failedCell = screen.getByTestId('page-cell-3');
    // disabled = 整格只读：cursor/aria 与真实行为一致（#289 光标判据不变）。
    expect(failedCell.className).toContain('cursor-not-allowed');
    expect(failedCell.className).not.toContain('cursor-pointer');
    expect(failedCell.getAttribute('aria-disabled')).toBe('true');

    fireEvent.click(failedCell);
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
  });
});