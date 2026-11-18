// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#355 iOS 重置：任务中心分页器换装（真稿 ios-tasks-center .pager）。
// - ds 下渲染数字页码胶囊分页器（NumericPager）：当前页 aria-current=page、
//   省略号、每页条数选择保留、点击页码回调 onGoPage；
// - classic 保持 PaginationRail 零漂移（jobs.showRange 文案 + pagination-rail
//   testid 在场，无数字页码）；数据口径（page/totalPages/total/pageSize）同源。
// 断言走结构与 props 透传（文案 i18n 键由编排者统一落盘，本文件不依赖具体文案）。

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { useUiTheme } from '@/theme/ui-theme';

import { JobsPagination, NumericPager, buildNumericPagerItems } from './components/jobs-pagination';

type PagerProps = Parameters<typeof JobsPagination>[0];

function renderPager(overrides: Partial<PagerProps> = {}) {
  const props: PagerProps = {
    page: 1,
    pageSize: 20,
    totalPages: 9,
    total: 165,
    rangeStart: 1,
    rangeEnd: 20,
    jumpPage: '',
    tableBusy: false,
    onGoPage: vi.fn(),
    onChangePageSize: vi.fn(),
    onJumpPageChange: vi.fn(),
    ...overrides,
  };
  const view = render(<JobsPagination {...props} />);
  return { props, ...view };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  useUiTheme.setState({ theme: 'classic' });
});

describe('Issue#355 ds 分页器换装（数字页码胶囊）', () => {
  it('ds 下渲染数字页码：当前页 aria-current=page、首末页+省略号在场、每页条数选择保留', () => {
    useUiTheme.setState({ theme: 'ds' });
    renderPager();

    const pager = screen.getByTestId('pagination-rail');
    const current = pager.querySelector('[aria-current="page"]');
    expect(current).toBeTruthy();
    expect(current?.textContent).toBe('1');
    // 当前页胶囊类名（样式钩子）
    expect(current?.classList.contains('ios-pager__pg--on')).toBe(true);
    // 尾页 + 省略号（totalPages=9、page=1 → 1 2 … 8 9）
    expect(within(pager as HTMLElement).getByText('9')).toBeTruthy();
    expect(within(pager as HTMLElement).getByText('…')).toBeTruthy();
    // 每页条数 Select 保留（Radix 触发器 jsdom 不暴露 combobox role，查类钩子）
    expect(pager.querySelector('.ios-pager__size')).toBeTruthy();
    // 信息行元素在场（文案由 i18n 键承载，见报告键清单）
    expect(pager.querySelector('.ios-pager__info')).toBeTruthy();
  });

  it('ds 下点击页码/下一页回调 onGoPage；tableBusy 时页码禁用', () => {
    useUiTheme.setState({ theme: 'ds' });
    const { props } = renderPager();

    const pager = screen.getByTestId('pagination-rail');
    fireEvent.click(within(pager as HTMLElement).getByText('2'));
    expect(props.onGoPage).toHaveBeenCalledWith(2);
    fireEvent.click(screen.getByTestId('pager-next'));
    expect(props.onGoPage).toHaveBeenLastCalledWith(2);

    // busy 态：翻页不可用
    cleanup();
    const busy = renderPager({ tableBusy: true, page: 3 });
    expect(screen.getByTestId('pager-next').hasAttribute('disabled')).toBe(true);
    const busyPager = screen.getByTestId('pagination-rail');
    fireEvent.click(busyPager.querySelector('[aria-current="page"]') as HTMLElement);
    expect(busy.props.onGoPage).not.toHaveBeenCalled();
  });

  it('classic 保持 PaginationRail（零漂移钉：showRange 文案在场、无数字页码结构）', () => {
    useUiTheme.setState({ theme: 'classic' });
    renderPager();

    expect(screen.queryByRole('button', { name: '第 1 页' })).toBeNull();
    expect(document.querySelector('.ios-pager')).toBeNull();
    // zh.ts 末位覆盖口径：'显示 {start}-{end} / 共 {total}'
    expect(screen.getByTestId('pagination-rail').textContent).toContain('显示 1-20 / 共 165');
    expect(screen.queryByTestId('pager-next')).toBeNull();
  });
});

describe('NumericPager：infoText 占位符与禁用边界', () => {
  it('直接传 infoText（调用方组好占位符）与自定义单位', () => {
    useUiTheme.setState({ theme: 'ds' });
    render(
      <NumericPager
        page={2}
        pageSize={10}
        total={35}
        totalPages={4}
        onGoPage={vi.fn()}
        onChangePageSize={vi.fn()}
        pageSizeOptions={[10, 20]}
        infoText="共 35 个任务 · 每页 10 条"
        perPageLabel="每页"
        itemsUnitLabel="条"
      />,
    );
    expect(document.querySelector('.ios-pager__info')?.textContent).toBe(
      '共 35 个任务 · 每页 10 条',
    );
    expect(document.querySelector('.ios-pager__size')).toBeTruthy();
  });

  it('单页时 prev/next 均禁用', () => {
    useUiTheme.setState({ theme: 'ds' });
    render(<NumericPager page={1} pageSize={10} total={5} totalPages={1} onGoPage={vi.fn()} />);
    expect(screen.getByTestId('pager-prev').hasAttribute('disabled')).toBe(true);
    expect(screen.getByTestId('pager-next').hasAttribute('disabled')).toBe(true);
  });
});

describe('buildNumericPagerItems：数字页码窗口', () => {
  it('≤7 页全展开', () => {
    expect(buildNumericPagerItems(3, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
  });

  it('>7 页收窗：首末页 + 当前页 ±1 + 省略号占位', () => {
    expect(buildNumericPagerItems(5, 20)).toEqual([1, 2, 'gap', 4, 5, 6, 'gap', 19, 20]);
    expect(buildNumericPagerItems(1, 9)).toEqual([1, 2, 'gap', 8, 9]);
    expect(buildNumericPagerItems(9, 9)).toEqual([1, 2, 'gap', 8, 9]);
  });
});
