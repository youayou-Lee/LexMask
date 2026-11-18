// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#355 验收期深化：ds 骨架行为钉（用户裁定「骨架补齐」+「恢复指标条」）。
// - ds 下 /jobs 渲染页面标题区（宋体大标题）与指标条（单条通栏白卡 4 等分），
//   指标数值与页签徽标同源（activeJobs+agent / awaitingReviewItems+agent / completedJobs / riskJobs）；
// - ds 下表格为定稿 8 列：合并统计列（已完成/总数 + 待审/失败角标）替代 4 个裸计数列，
//   副信息行（N 个文件）与人性化时间（今天/昨天）；
// - classic 分支零新 DOM：无指标条/标题区、表格保持 11 列与 4 计数列 testid
//   （src/test-setup.ts 已把 vitest 环境默认钉 classic，本文件显式切 ds 验证）。

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useI18n } from '@/i18n';
import { useUiTheme } from '@/theme/ui-theme';
import type { JobProgress, JobSummary } from '@/services/jobsApi';

import { TasksPage } from './tasks-page';
import { JobsTable } from './components/jobs-table';

const { mockedUseJobs, resetMockedJobsState } = vi.hoisted(() => {
  const mockState = { tab: 'all', initialized: false };
  const mockedJobs = {
    tab: 'all',
    rows: [] as unknown[],
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
      awaitingReviewJobs: 3,
      completedJobs: 40,
      riskJobs: 1,
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
    items: [] as unknown[],
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
  const actual = await importOriginal<typeof import('./hooks/use-jobs')>();
  return { ...actual, useJobs: mockedUseJobs };
});
vi.mock('@/features/history', () => ({
  HistoryView: () => <div data-testid="history-page" />,
}));

const CREATED = '2026-01-01T00:00:00Z';

function dsJob(id: string, updatedIso?: string): JobSummary {
  const progress: JobProgress = {
    total_items: 3,
    pending: 0,
    processing: 0,
    queued: 0,
    parsing: 0,
    ner: 0,
    vision: 0,
    awaiting_review: 1,
    review_approved: 0,
    redacting: 0,
    completed: 2,
    failed: 0,
  };
  return {
    id,
    job_type: 'single',
    title: `案件-${id}.pdf`,
    status: 'awaiting_review',
    skip_item_review: false,
    config: { single_file_id: `file-${id}` },
    error_message: null,
    created_at: CREATED,
    updated_at: updatedIso ?? new Date().toISOString(),
    progress,
  };
}

function renderTasks(initialEntry = '/jobs') {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <TasksPage />
    </MemoryRouter>,
  );
}

function renderTable(rows: JobSummary[], dsMode: boolean) {
  useUiTheme.setState({ theme: dsMode ? 'ds' : 'classic' });
  return render(
    <MemoryRouter>
      <JobsTable
        rows={rows}
        loading={false}
        refreshing={false}
        pageSize={10}
        deletingJobId={null}
        tableBusy={false}
        onDelete={vi.fn()}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
  useUiTheme.setState({ theme: 'ds' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  resetMockedJobsState();
  mockedAgentTasks.counts = { active: 0, awaiting_review: 0 };
  useUiTheme.setState({ theme: 'classic' });
});

describe('Issue#355 ds 骨架：页面标题区 + 指标条', () => {
  it('ds 下渲染标题区与指标条，四格数值与页签徽标同源（activeJobs+agent / awaitingReviewItems+agent / completedJobs / riskJobs）', () => {
    mockedAgentTasks.counts = { active: 1, awaiting_review: 2 };
    renderTasks('/jobs');
    // 标题区（定稿宋体大标题）
    expect(screen.getByTestId('ds-page-header').textContent).toContain('任务中心');
    // 指标条四格：7+1=8 / 3+2=5 / 40 / 1
    expect(screen.getByTestId('ds-metric-active').textContent).toContain('8');
    expect(screen.getByTestId('ds-metric-review').textContent).toContain('5');
    expect(screen.getByTestId('ds-metric-done').textContent).toContain('40');
    expect(screen.getByTestId('ds-metric-risk').textContent).toContain('1');
    // 页签徽标仍在（定稿两者共存，用户拍板口径）
    expect(screen.getByTestId('tasks-tab-review').textContent).toContain('5');
  });

  it('classic 下不渲染标题区与指标条（零新 DOM）', () => {
    useUiTheme.setState({ theme: 'classic' });
    const { container } = renderTasks('/jobs');
    expect(screen.queryByTestId('ds-page-header')).toBeNull();
    expect(screen.queryByTestId('ds-metrics-strip')).toBeNull();
    // classic 表格标题在场（jobs-table-heading 仅在 ds 经 CSS 隐藏，DOM 两态都在）
    expect(container.querySelector('.jobs-table-heading')).toBeTruthy();
  });
});

describe('Issue#355 ds 骨架：表格重构', () => {
  it('ds 行：合并统计列（已完成/总数 + 待审角标）替代 4 计数列；副信息行；人性化时间', () => {
    const job = dsJob('j1');
    renderTable([job], true);
    // 4 计数列 testid 清零（ds 不渲染）
    expect(screen.queryByTestId('job-total-count-j1')).toBeNull();
    expect(screen.queryByTestId('job-completed-count-j1')).toBeNull();
    // 合并统计列：2 / 3 + 待审 1
    const stats = screen.getByTestId('job-stats-j1');
    expect(stats.textContent).toContain('2 / 3');
    expect(stats.textContent).toContain('待审 1');
    // 副信息行（3 个文件）
    expect(screen.getByTestId('jobs-table-body').textContent).toContain('3 个文件');
    // 人性化时间（updated_at=现在 ⇒ 今天）
    expect(screen.getByTestId('jobs-table-body').textContent).toContain('今天');
  });

  it('classic 行：保持 4 计数列 testid 与完整本地化时间（零漂移钉）', () => {
    const job = dsJob('j1', '2026-01-15T08:30:00Z');
    renderTable([job], false);
    expect(screen.getByTestId('job-total-count-j1')).toBeTruthy();
    expect(screen.getByTestId('job-completed-count-j1')).toBeTruthy();
    expect(screen.getByTestId('job-awaiting-review-count-j1')).toBeTruthy();
    expect(screen.getByTestId('job-abnormal-count-j1')).toBeTruthy();
    expect(screen.queryByTestId('job-stats-j1')).toBeNull();
    // classic 时间不走人性化（2026/1/15 日期字符串在场）
    expect(screen.getByTestId('jobs-table-body').textContent).not.toContain('今天');
  });
});
