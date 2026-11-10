// Copyright 2026 LexMask Contributors

// Issue#262 A1：任务中心合并页（/jobs = 工单 + 结果双视图，/history 重定向）。
// 本测试的被测对象是外层页签容器：URL ?view= 缺省 active（缺省不污染 URL）、
// view→jobs 内层 tab 一次性预置、done 视图惰性挂载 History（不抬 /files）、
// active/review 页签计数取 jobs.pageMetrics。
// 工单 hook（use-jobs）与结果视图（@/features/history）整体 mock——mock 字段以
// use-jobs.ts 返回形状（:686-727）与 JobsFilters/JobsTable 的 props 消费清单
// 逐键补全（惯例参考 advanced-hub.test.tsx / app-sidebar.nav.test.tsx）。

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';

import { TasksPage } from './tasks-page';

const { mockedJobs } = vi.hoisted(() => ({
  mockedJobs: {
    // use-jobs.ts:686-727 返回形状——JobsLedgerView / TasksPage 实际消费的键。
    tab: 'all',
    rows: [],
    total: 0,
    page: 1,
    pageSize: 10,
    rowsPageSize: 10,
    jumpPage: '',
    loading: false,
    tableLoading: false,
    refreshing: false,
    cleanupConfirmOpen: false,
    err: null,
    notice: null,
    deletingJobId: null,
    cleanupLoading: false,
    deleteCandidate: null,
    expandedJobIds: new Set<string>(),
    jobDetails: {},
    detailLoadingIds: new Set<string>(),
    requeueingJobId: null,
    totalPages: 1,
    tableBusy: false,
    interactionLocked: false,
    rangeStart: 0,
    rangeEnd: 0,
    pageMetrics: {
      totalJobs: 12,
      activeJobs: 7,
      awaitingReviewItems: 3,
      completedItems: 40,
      riskItems: 1,
    },
    changeTab: vi.fn(),
    refreshList: vi.fn(),
    goPage: vi.fn(),
    changePageSize: vi.fn(),
    setJumpPage: vi.fn(),
    toggleExpand: vi.fn(),
    requestDelete: vi.fn(),
    cancelDelete: vi.fn(),
    confirmDelete: vi.fn(),
    onRequeueFailed: vi.fn(),
    onCleanup: vi.fn(),
    setCleanupConfirmOpen: vi.fn(),
  },
}));

vi.mock('./hooks/use-jobs', async (importOriginal) => {
  // 只换 useJobs；PAGE_SIZE_OPTIONS / canDeleteJob 等运行时导出保留原实现
  // （jobs-pagination / jobs-table 仍从本模块消费）。
  const actual = await importOriginal<typeof import('./hooks/use-jobs')>();
  return { ...actual, useJobs: () => mockedJobs };
});
vi.mock('@/features/history', () => ({
  // History 视图整体 mock，断言惰性挂载（仅 view=done 渲染，避免双列表请求）。
  HistoryView: () => <div data-testid="history-page" />,
}));

/** 观察 URL：view↔URL 回写的证据侧（与 settings-hub / use-history-data 的
 * searchParams 同步范式对应） */
function LocationProbe() {
  const location = useLocation();
  return (
    <div
      data-testid="location-probe"
      data-pathname={location.pathname}
      data-search={location.search}
    />
  );
}

function renderTasks(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <TasksPage />
    </MemoryRouter>,
  );
}

/** 激活页签：Radix Tabs.Trigger 的激活事件是 onMouseDown（button=0、非 ctrl） */
function activateTab(testId: string) {
  fireEvent.mouseDown(screen.getByTestId(testId), { button: 0 });
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#262：任务中心合并页', () => {
  it('缺省 view=active 渲染工单视图，不挂 History；active 页签带 pageMetrics.activeJobs 计数', () => {
    renderTasks('/jobs');
    expect(screen.getByTestId('tasks-page')).toBeTruthy();
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.queryByTestId('history-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-active').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('tasks-tab-review').getAttribute('aria-selected')).toBe('false');
    expect(screen.getByTestId('tasks-tab-done').getAttribute('aria-selected')).toBe('false');
    // 页签计数取 pageMetrics（active=7 / review=3）
    expect(screen.getByTestId('tasks-tab-active').textContent).toContain('7');
    expect(screen.getByTestId('tasks-tab-review').textContent).toContain('3');
    // view=active 预置 jobs 内层 tab（useJobs 初始 'all' → 'active'）
    expect(mockedJobs.changeTab).toHaveBeenCalledWith('active');
    // 缺省视图不污染 URL
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('');
  });

  it('view=done 渲染结果视图，工单表卸载（避免双请求），done 页签不显示计数', () => {
    renderTasks('/jobs?view=done');
    expect(screen.getByTestId('history-page')).toBeTruthy();
    expect(screen.queryByTestId('jobs-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-done').getAttribute('aria-selected')).toBe('true');
    // done 视图不预置 jobs 内层 tab（工单 hook 不因深链切表）
    expect(mockedJobs.changeTab).not.toHaveBeenCalled();
    // done 页签不显示计数（history hook 惰性挂载，无来源）
    expect(screen.getByTestId('tasks-tab-done').textContent).not.toContain('7');
    expect(screen.getByTestId('tasks-tab-done').textContent).not.toContain('3');
  });

  it('view=review 预置 jobs 内层 tab awaiting_review（URL→tab 一次性预置）', () => {
    renderTasks('/jobs?view=review');
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.queryByTestId('history-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-review').getAttribute('aria-selected')).toBe('true');
    expect(mockedJobs.changeTab).toHaveBeenCalledWith('awaiting_review');
  });

  it('非法 ?view= 值回落工单视图（缺省 active）', () => {
    renderTasks('/jobs?view=bogus');
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.queryByTestId('history-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-active').getAttribute('aria-selected')).toBe('true');
  });

  it('点击页签回写 URL：缺省 view 移除参数（tab→URL，与 D3 同步惯例一致）', () => {
    renderTasks('/jobs');
    activateTab('tasks-tab-done');
    expect(screen.getByTestId('history-page')).toBeTruthy();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('?view=done');

    activateTab('tasks-tab-review');
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('?view=review');

    // 切回缺省页签：?view= 移除
    activateTab('tasks-tab-active');
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('');
  });

  it('done 深链透传：?source=&jobId= 在页签往返中保留（/history 旧深链经重定向后由 HistoryView 消费）', () => {
    renderTasks('/jobs?view=done&source=batch&jobId=j-1');
    expect(screen.getByTestId('history-page')).toBeTruthy();

    // 切走再切回：source/jobId 原样保留，view 重新写回
    activateTab('tasks-tab-active');
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe(
      '?source=batch&jobId=j-1',
    );
    activateTab('tasks-tab-done');
    expect(screen.getByTestId('history-page')).toBeTruthy();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe(
      '?source=batch&jobId=j-1&view=done',
    );
  });
});
