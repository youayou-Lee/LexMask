// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#289 · 失败页「看」与「重试」拆分的组件级契约。
//
// 钉四件事：
//  1) 详情面板内重试按钮（page-detail-retry）四态：可重试 / 重试中 / 识别中(忙) / 已终态，
//     各自的禁用位与原因提示；重试中不渲染原因提示（F4：底部 hint 与面板同口径另钉在
//     issue257-page-retry 的终态用例）；
//  2) M3 杀手：面板重试按钮只属于失败页——传 onRetryPage 点**成功页**开面板，绝不渲染重试入口；
//  3) 格子交互语义（设计 (c)）：失败格恒可点展开，cursor/aria 不再随 retryability 谎报；
//     matrix 禁用时反向（夹具 awaiting_review+disabled，实现前真红）；pending 格
//     aria-disabled 翻转为有意行为面变化（防回退钉）；
//  4) 再次点击红格收起面板不回归。
//
// 实现前必红的用例（AC0 / AC2-c 红性先证）与变异映射见方案
// LexMask-workspace/docs/plans/2026-11-13-issue289-failed-page-interaction.md（AC8 M1-M5）。

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

import { BatchPageMatrix } from './batch-page-matrix';

afterEach(() => cleanup());

function makeStatus(overrides: Partial<ItemPageStatus> = {}): ItemPageStatus {
  return {
    item_id: 'item-289',
    job_id: 'job-289',
    file_id: 'file-289',
    paged: true,
    page_count: 5,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-13T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
      { page: 3, state: 'failed', reason: 'ner boom' },
      { page: 4, state: 'pending' },
      { page: 5, state: 'success' },
    ],
    failed_pages: [3],
    ...overrides,
  };
}

function renderMatrix(
  status: ItemPageStatus,
  onChange: (next: Set<number>) => void = vi.fn(),
  onRetryPage: (page: number) => void = vi.fn(),
  extra: { disabled?: boolean; retrying?: boolean } = {},
) {
  return render(
    <BatchPageMatrix
      status={status}
      selected={new Set()}
      onChange={onChange}
      onRetryPage={onRetryPage}
      retrying={extra.retrying ?? false}
      disabled={extra.disabled ?? false}
    />,
  );
}

describe('详情面板内重试按钮四态（Issue#289）', () => {
  it('awaiting_review：按钮可用，点击只把该页交给 onRetryPage；红格点击不发请求、不进选择集', async () => {
    const onChange = vi.fn();
    const onRetryPage = vi.fn();
    renderMatrix(makeStatus(), onChange, onRetryPage);

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-3'));
    });
    // 拆分主判据（M1/M5 杀手载体）：格子点击既不发重试也不改选择。
    expect(onRetryPage).not.toHaveBeenCalled();
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('3');

    const button = screen.getByTestId('page-detail-retry');
    expect(button.hasAttribute('disabled')).toBe(false);
    expect(button.textContent).toContain('重试这一页');

    await act(async () => {
      fireEvent.click(button);
    });
    expect(onRetryPage).toHaveBeenCalledWith(3);
  });

  it('识别中：按钮禁用 + 忙态提示，点按钮不触发重试', async () => {
    const onRetryPage = vi.fn();
    renderMatrix(
      makeStatus({ recognition_state: 'running', item_status: 'processing' }),
      vi.fn(),
      onRetryPage,
    );

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-3'));
    });
    const button = screen.getByTestId('page-detail-retry');
    expect(button.hasAttribute('disabled')).toBe(true);
    expect(screen.getByTestId('page-detail-retry-unavailable').textContent).toContain('需先完成识别');

    await act(async () => {
      fireEvent.click(button);
    });
    expect(onRetryPage).not.toHaveBeenCalled();
  });

  it('已终态：按钮禁用 + 终态提示，与忙态文案区分', async () => {
    renderMatrix(makeStatus({ item_status: 'completed' }));

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-3'));
    });
    expect(screen.getByTestId('page-detail-retry').hasAttribute('disabled')).toBe(true);
    const hint = screen.getByTestId('page-detail-retry-unavailable');
    expect(hint.textContent).toContain('终态');
    expect(hint.textContent).not.toContain('需先完成识别');
  });

  it('重试在飞：按钮禁用 + 「重试中…」，不渲染原因提示', async () => {
    renderMatrix(makeStatus(), vi.fn(), vi.fn(), { retrying: true });

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-3'));
    });
    const button = screen.getByTestId('page-detail-retry');
    expect(button.hasAttribute('disabled')).toBe(true);
    expect(button.textContent).toContain('重试中');
    expect(screen.queryByTestId('page-detail-retry-unavailable')).toBeNull();
  });
});

describe('面板重试按钮只属于失败页（M3 杀手）', () => {
  it('传 onRetryPage 时点成功页开面板：无重试按钮，选择照常', async () => {
    const onChange = vi.fn();
    const onRetryPage = vi.fn();
    renderMatrix(makeStatus(), onChange, onRetryPage);

    await act(async () => {
      fireEvent.click(screen.getByTestId('page-cell-1'));
    });
    expect(screen.getByTestId('page-detail-panel').getAttribute('data-page')).toBe('1');
    // M3：若按钮渲染条件丢了 state==='failed'，成功页面板会冒出重试按钮 ⇒ 此断言红。
    expect(screen.queryByTestId('page-detail-retry')).toBeNull();
    expect(screen.queryByTestId('page-detail-retry-unavailable')).toBeNull();
    // 成功页选择行为不动（#257）。
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onRetryPage).not.toHaveBeenCalled();
  });
});

describe('格子交互语义（Issue#289 设计(c)：cursor/aria 不再随 retryability 谎报）', () => {
  it('终态条目的失败格：恒可点（cursor-pointer）且读屏不禁用（aria-disabled=false）', () => {
    renderMatrix(makeStatus({ item_status: 'completed' }));
    const cell = screen.getByTestId('page-cell-3');
    // 实现前必红：现行为 !retryable 时是 cursor-not-allowed + aria-disabled=true。
    expect(cell.className).toContain('cursor-pointer');
    expect(cell.getAttribute('aria-disabled')).toBe('false');
  });

  it('matrix 禁用时失败格：cursor-not-allowed 且 aria-disabled=true（夹具 awaiting_review+disabled，实现前真红）', () => {
    renderMatrix(makeStatus(), vi.fn(), vi.fn(), { disabled: true });
    const cell = screen.getByTestId('page-cell-3');
    expect(cell.className).toContain('cursor-not-allowed');
    // 禁用是唯一光标语义：不允许 cursor-pointer 与 cursor-not-allowed 双类并存（实现前必红：双类并存）。
    expect(cell.className).not.toContain('cursor-pointer');
    // 实现前必红：现行为 failed+retryable 时 aria-disabled=false（disabled prop 不参与该分支）。
    expect(cell.getAttribute('aria-disabled')).toBe('true');
  });

  it('pending 格 aria-disabled 翻转（有意行为面变化，防回退钉）：可用态为 false', () => {
    renderMatrix(makeStatus());
    // 实现前必红：现行为 :352 对非 success 态一律 aria-disabled=true。
    expect(screen.getByTestId('page-cell-4').getAttribute('aria-disabled')).toBe('false');
  });
});

describe('展开/收起不回归', () => {
  it('再次点击红格收起面板（aria-expanded 翻转）', async () => {
    renderMatrix(makeStatus());
    const cell = screen.getByTestId('page-cell-3');
    await act(async () => {
      fireEvent.click(cell);
    });
    expect(screen.getByTestId('page-detail-panel')).toBeTruthy();
    expect(cell.getAttribute('aria-expanded')).toBe('true');
    await act(async () => {
      fireEvent.click(cell);
    });
    expect(screen.queryByTestId('page-detail-panel')).toBeNull();
    expect(cell.getAttribute('aria-expanded')).toBe('false');
  });
});
