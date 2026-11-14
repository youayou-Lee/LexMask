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

const { mockedJobs, mockedUseJobs, resetMockedJobsState } = vi.hoisted(() => {
  const mockState = { tab: 'all', initialized: false };
  const mockedJobs = {
    // use-jobs.ts 返回形状——JobsLedgerView / TasksPage 实际消费的键。
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
  };
  // Issue#268：参数化 + 有状态 mock——initialTab 只在首次实例化生效（模拟真 hook 的
  // useState 初值语义），此后 tab 由 changeTab 驱动。纯回显（每次渲染都回显 initialTab）
  // 会让「挂载后点击外层页签仍调 changeTab」断言失效，无法同时钉住预置 effect 的两个职责。
  mockedJobs.changeTab = vi.fn((next: string) => {
    mockState.tab = next;
  });
  const mockedUseJobs = vi.fn((opts?: { initialTab?: string; pollingEnabled?: boolean }) => {
    if (!mockState.initialized) {
      mockState.tab = opts?.initialTab ?? 'all';
      mockState.initialized = true;
    }
    return { ...mockedJobs, tab: mockState.tab };
  });
  return {
    mockedJobs,
    mockedUseJobs,
    resetMockedJobsState: () => {
      mockState.tab = 'all';
      mockState.initialized = false;
    },
  };
});

const { mockedAgentTasks } = vi.hoisted(() => ({
  mockedAgentTasks: {
    items: [] as Array<{
      task_id: string;
      filename: string;
      state: string;
      stage: string;
      message: string;
      pages_done: number;
      pages_total: number;
      output_file_id?: string | null;
      created_at: string;
      updated_at: string;
    }>,
    counts: { active: 0, awaiting_review: 0 },
    loading: false,
    err: null as string | null,
    refresh: vi.fn(),
  },
}));

vi.mock('./hooks/use-agent-md-tasks', () => ({
  useAgentMdTasks: () => mockedAgentTasks,
}));

vi.mock('./hooks/use-jobs', async (importOriginal) => {
  // 只换 useJobs；PAGE_SIZE_OPTIONS / canDeleteJob 等运行时导出保留原实现
  // （jobs-pagination / jobs-table 仍从本模块消费）。
  const actual = await importOriginal<typeof import('./hooks/use-jobs')>();
  return { ...actual, useJobs: mockedUseJobs };
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
  resetMockedJobsState();
  mockedAgentTasks.items = [];
  mockedAgentTasks.counts = { active: 0, awaiting_review: 0 };
  mockedAgentTasks.err = null;
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
    // Issue#268：initialTab 由 URL ?view= 注入——挂载即 'active'（不再「初始 'all' →
    // changeTab 自纠」的双 GET），预置 effect 挂载时 no-op
    expect(mockedUseJobs).toHaveBeenCalledWith(
      expect.objectContaining({ initialTab: 'active', pollingEnabled: true }),
    );
    expect(mockedJobs.changeTab).not.toHaveBeenCalled();
    // 缺省视图不污染 URL
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('');
  });

  it('view=done 渲染结果视图，工单表卸载（避免双请求），done 页签不显示计数', () => {
    renderTasks('/jobs?view=done');
    expect(screen.getByTestId('history-page')).toBeTruthy();
    expect(screen.queryByTestId('jobs-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-done').getAttribute('aria-selected')).toBe('true');
    // done 视图不预置 jobs 内层 tab（工单 hook 不因深链切表）；
    // Issue#268：轮询关闭（pollingEnabled=false），initialTab 保持 'all' 供页签徽标首挂供数
    expect(mockedJobs.changeTab).not.toHaveBeenCalled();
    expect(mockedUseJobs).toHaveBeenCalledWith(
      expect.objectContaining({ initialTab: 'all', pollingEnabled: false }),
    );
    // done 页签无计数 pill 元素（收紧：不再依赖文案 not.toContain 的巧合，
    // 直接断言计数 span（tabular-nums）不存在——history hook 惰性挂载，无计数来源）
    expect(screen.getByTestId('tasks-tab-done').querySelector('span.tabular-nums')).toBeNull();
  });

  it('view=review 预置 jobs 内层 tab awaiting_review（URL→tab 一次性预置）', () => {
    renderTasks('/jobs?view=review');
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.queryByTestId('history-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-review').getAttribute('aria-selected')).toBe('true');
    // Issue#268：review 深链 initialTab='awaiting_review' 注入，挂载时预置 no-op
    //（挂载后页签切换仍调 changeTab 的职责由下方「点击页签回写 URL」用例覆盖）
    expect(mockedUseJobs).toHaveBeenCalledWith(
      expect.objectContaining({ initialTab: 'awaiting_review', pollingEnabled: true }),
    );
    expect(mockedJobs.changeTab).not.toHaveBeenCalled();
  });

  it('非法 ?view= 值回落工单视图（缺省 active）', () => {
    renderTasks('/jobs?view=bogus');
    expect(screen.getByTestId('jobs-page')).toBeTruthy();
    expect(screen.queryByTestId('history-page')).toBeNull();
    expect(screen.getByTestId('tasks-tab-active').getAttribute('aria-selected')).toBe('true');
    // Issue#268：非法 view 经 readTasksView 归一后注入 initialTab='active'（杀 M6）
    expect(mockedUseJobs).toHaveBeenCalledWith(
      expect.objectContaining({ initialTab: 'active' }),
    );
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

describe('Issue#282：供料任务并入任务中心', () => {
  it('进行中/待审阅供料任务按视图分流，计数并入 active/review 徽标', () => {
    mockedAgentTasks.counts = { active: 2, awaiting_review: 1 };
    const base = {
      stage: 'ner',
      message: '',
      pages_done: 2,
      pages_total: 2,
      output_file_id: null,
      created_at: '2026-11-12T04:00:00Z',
      updated_at: '2026-11-12T04:00:00Z',
    };
    mockedAgentTasks.items = [
      { ...base, task_id: 't-review', filename: '借条.pdf', state: 'mapping_ready' },
      { ...base, task_id: 't-active', filename: '卷宗.pdf', state: 'parsing' },
    ];

    renderTasks('/jobs');
    expect(screen.getByTestId('agent-md-task-panel')).toBeTruthy();
    // active 视图只列运行态，待复核行不混入（review #282-1）
    expect(screen.getByTestId('agent-md-task-t-active').getAttribute('href')).toBe(
      '/agent-md?taskId=t-active',
    );
    expect(screen.queryByTestId('agent-md-task-t-review')).toBeNull();
    // 工单 7 + counts.active 2 = 9；面板只列当前视图可见行（1 条，另一个 active 不在 items 内）
    expect(screen.getByTestId('tasks-tab-active').textContent).toContain('9');
    expect(screen.getAllByTestId(/^agent-md-task-t-/)).toHaveLength(1);
    expect(screen.getByTestId('tasks-tab-review').textContent).toContain('4');

    activateTab('tasks-tab-review');
    expect(screen.getByTestId('agent-md-task-t-review')).toBeTruthy();
    expect(screen.queryByTestId('agent-md-task-t-active')).toBeNull();
  });

  it('failed 任务区分「已中断」与普通失败', () => {
    mockedAgentTasks.counts = { active: 2, awaiting_review: 0 };
    const base = {
      stage: 'ner',
      pages_done: 0,
      pages_total: 3,
      output_file_id: null,
      created_at: '2026-11-12T04:00:00Z',
      updated_at: '2026-11-12T04:00:00Z',
    };
    mockedAgentTasks.items = [
      {
        ...base,
        task_id: 't-int',
        filename: '中断.pdf',
        state: 'failed',
        message: 'interrupted: 服务重启，任务已中断，请重新发起',
      },
      {
        ...base,
        task_id: 't-err',
        filename: '失败.pdf',
        state: 'failed',
        message: 'parse_no_text: 空文档',
      },
    ];
    renderTasks('/jobs');
    expect(screen.getByTestId('agent-md-task-t-int').textContent).toContain('已中断');
    expect(screen.getByTestId('agent-md-task-t-err').textContent).toContain('失败');
  });

  it('无进行中供料任务时不渲染面板', () => {
    renderTasks('/jobs');
    expect(screen.queryByTestId('agent-md-task-panel')).toBeNull();
  });
});
