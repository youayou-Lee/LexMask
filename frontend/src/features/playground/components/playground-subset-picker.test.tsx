// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// 2026-11-16 用户口径：「只要 2、3、4 页就能先匿名化」——子集预览前必须先选页。
//
// 判据：
//  - 只有已处理页可选（未处理页 disabled），与后端 subset_pages_not_ready 同口径；
//  - 默认全选已处理页（保持「先匿名化已处理的 N/M 页」一键语义）；
//  - 确认回吐升序页号并关闭；全选/清空只影响选择集。

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PlaygroundSubsetPicker } from './playground-subset-picker';

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

function renderPicker(overrides: Partial<Parameters<typeof PlaygroundSubsetPicker>[0]> = {}) {
  const props = {
    open: true,
    onOpenChange: vi.fn(),
    pages: [1, 3, 4],
    total: 6,
    onConfirm: vi.fn(),
    ...overrides,
  };
  render(<PlaygroundSubsetPicker {...props} />);
  return props;
}

describe('PlaygroundSubsetPicker（2026-11-16 子集选页）', () => {
  it('已处理页可选、未处理页 disabled', () => {
    renderPicker();

    expect(screen.getByTestId('playground-subset-page-1').getAttribute('data-selectable')).toBe(
      'true',
    );
    expect(screen.getByTestId('playground-subset-page-3').getAttribute('data-selectable')).toBe(
      'true',
    );
    expect(screen.getByTestId('playground-subset-page-2').getAttribute('data-selectable')).toBe(
      'false',
    );
    expect(screen.getByTestId('playground-subset-page-2').hasAttribute('disabled')).toBe(true);
  });

  it('默认全选已处理页', () => {
    renderPicker();

    expect(screen.getByTestId('playground-subset-page-1').getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('playground-subset-page-4').getAttribute('aria-pressed')).toBe('true');
  });

  it('取消勾选后确认 ⇒ 只回吐剩下的页（升序）', () => {
    const props = renderPicker();

    fireEvent.click(screen.getByTestId('playground-subset-page-1'));
    fireEvent.click(screen.getByTestId('playground-subset-picker-confirm'));

    expect(props.onConfirm).toHaveBeenCalledWith([3, 4]);
    expect(props.onOpenChange).toHaveBeenCalledWith(false);
  });

  it('「清空」后确认按钮禁用（不能提交空集）', () => {
    renderPicker();

    fireEvent.click(screen.getByTestId('playground-subset-picker-none'));

    const confirm = screen.getByTestId('playground-subset-picker-confirm') as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it('「全选」回到全部已处理页', () => {
    const props = renderPicker();

    fireEvent.click(screen.getByTestId('playground-subset-picker-none'));
    fireEvent.click(screen.getByTestId('playground-subset-picker-all'));
    fireEvent.click(screen.getByTestId('playground-subset-picker-confirm'));

    expect(props.onConfirm).toHaveBeenCalledWith([1, 3, 4]);
  });

  it('一页都没处理时不渲染页格，给空态文案', () => {
    renderPicker({ pages: [] });

    expect(screen.queryByTestId('playground-subset-picker-grid')).toBeNull();
    expect(screen.getByTestId('playground-subset-picker-empty')).toBeTruthy();
  });

  it('重开时按最新页账重置（识别推进后旧选择不残留）', () => {
    // 门⑥ r1 M8：必须走真正的 close → open（同树 rerender），而不是 cleanup + 重挂载。
    // 实现依赖 Radix DialogContent 关闭时卸载，forceMount/关闭动画窗口下重开会漏。
    const props = {
      open: true,
      onOpenChange: vi.fn(),
      pages: [1, 3, 4],
      total: 6,
      onConfirm: vi.fn(),
    };
    const { rerender } = render(<PlaygroundSubsetPicker {...props} />);
    fireEvent.click(screen.getByTestId('playground-subset-page-4'));

    // 关闭（页账仍是旧的），再用更大的页账重开
    rerender(<PlaygroundSubsetPicker {...props} open={false} />);
    rerender(<PlaygroundSubsetPicker {...props} pages={[1, 3, 4, 5]} />);

    expect(screen.getByTestId('playground-subset-page-5').getAttribute('aria-pressed')).toBe('true');
  });

  it('识别刚收尾（候选页清空）时文案指向全卷入口', () => {
    renderPicker({ pages: [], total: 86, recognitionActive: false });

    expect(screen.getByTestId('playground-subset-picker-empty').textContent).toContain(
      '识别已结束',
    );
  });

  it('识别在飞但还没有页完成时保持「稍候再试」文案', () => {
    renderPicker({ pages: [], total: 86, recognitionActive: true });

    expect(screen.getByTestId('playground-subset-picker-empty').textContent).toContain(
      '还没有已处理的页',
    );
  });
});
