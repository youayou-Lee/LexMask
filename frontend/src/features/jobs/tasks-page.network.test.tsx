// Copyright 2026 LexMask Contributors

// Issue#268：tasks-page 首挂恰一次 GET /jobs + done 视图停轮询的网络层断言。
//
// 被测对象是真实 useJobs + TasksPage 预置 effect 的集成（tasks-page.test.tsx 整体
// mock 了 useJobs，vi.mock 是文件级的，故新开文件避免 mock 冲突）。
// 计数口径：1 次 listJobs = 1 次 GET /jobs（jobsApi.ts listJobs → get('/jobs') 1:1）；
// 邻页 prefetch 在 test MODE 已关（use-jobs.ts scheduleAdjacentJobsPrefetch）。
//
// 红性基线（实现未改时，门①两轮独立探针实证，2026-11-13）：
//   缺省 /jobs 首挂 listJobs=2（status 序列 [undefined,'active']）、?view=review=2、
//   done(+running rows) 首挂 1 但 900ms 轮询涨到 2→3、done+空页 advance 2700ms 恒 1。
// 环境钉注：
//   - 不包 StrictMode——生产构建无双跑效应；包上会把挂载 effect 双跑、恰 1 次断言
//     放大成 4（假红）。测试环境已实证无 StrictMode（setupFiles 空、StrictMode 仅
//     main.tsx 生产入口；探针计数=2 非 4）。
//   - localStorage.clear() 进 beforeEach（jobs:list-cache:v1 残留会改变 silent 分支）。
//   - mock 卫生：listJobs 返回 total ≤ page_size × page（此处 total=jobs.length、
//     page=1、page_size=10），否则 load 的「目标页超总页数重发」分支
//     （use-jobs.ts targetPage > resolvedTotalPages）会污染恰一次计数。

// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { TasksPage } from './tasks-page';
import { listJobs, type JobSummary } from '@/services/jobsApi';

const listJobsMock = vi.fn();

vi.mock('@/services/jobsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/jobsApi')>();
  return { ...actual, listJobs: (...args: Parameters<typeof listJobs>) => listJobsMock(...args) };
});
vi.mock('@/features/history', () => ({
  HistoryView: () => <div data-testid="history-page" />,
}));
vi.mock('@/features/agent-md/api', () => ({
  agentMdApi: {
    list: vi.fn(async () => ({ items: [], counts: { active: 0, awaiting_review: 0 } })),
  },
}));

const EMPTY_STATS = {
  total_jobs: 0,
  draft_jobs: 0,
  active_jobs: 0,
  awaiting_review_jobs: 0,
  completed_jobs: 0,
  risk_jobs: 0,
  total_items: 0,
  active_items: 0,
  awaiting_review_items: 0,
  completed_items: 0,
  risk_items: 0,
};

/** mock 卫生：total=jobs.length ≤ page_size×page，不触发超页重发分支。 */
function jobsPage(jobs: JobSummary[]) {
  return {
    jobs,
    total: jobs.length,
    page: 1,
    page_size: 10,
    stats: {
      ...EMPTY_STATS,
      total_jobs: jobs.length,
      active_jobs: jobs.length,
      active_items: 2,
      total_items: 2,
    },
  };
}

/** 最小 running 态 JobSummary——jobsPollSignature/hasRefreshableJobWork/JobsTable 行渲染够用。 */
function runningJob(): JobSummary {
  return {
    id: 'j-running',
    job_type: 'single',
    title: '运行中卷',
    status: 'running',
    skip_item_review: false,
    config: {},
    created_at: '2026-11-13T00:00:00Z',
    updated_at: '2026-11-13T00:00:00Z',
    progress: {
      total_items: 2,
      pending: 0,
      processing: 1,
      queued: 1,
      parsing: 0,
      ner: 0,
      vision: 0,
      awaiting_review: 0,
      review_approved: 0,
      redacting: 0,
      completed: 0,
      failed: 0,
      cancelled: 0,
    },
    nav_hints: { item_count: 2 },
  };
}

function renderTasks(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <TasksPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  window.localStorage.clear();
  listJobsMock.mockReset();
  listJobsMock.mockImplementation(async () => jobsPage([]));
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe('Issue#268：tasks-page 首挂恰一次 GET /jobs', () => {
  it('缺省 /jobs 首挂 listJobs 恰 1 次，status=active（修复前=2：[undefined, active]）', () => {
    renderTasks('/jobs');
    expect(listJobsMock).toHaveBeenCalledTimes(1);
    expect(listJobsMock.mock.calls[0][0]).toMatchObject({ page: 1, page_size: 10 });
    expect(listJobsMock.mock.calls[0][0].status).toBe('active');
  });

  it('?view=review 首挂恰 1 次，status=awaiting_review（修复前=2）', () => {
    renderTasks('/jobs?view=review');
    expect(listJobsMock).toHaveBeenCalledTimes(1);
    expect(listJobsMock.mock.calls[0][0]).toMatchObject({
      status: 'awaiting_review',
      page: 1,
    });
  });

  it('?view=bogus 回落 active：恰 1 次且 status=active（杀 M6：initialTab 必须复用 readTasksView 归一）', () => {
    renderTasks('/jobs?view=bogus');
    expect(listJobsMock).toHaveBeenCalledTimes(1);
    expect(listJobsMock.mock.calls[0][0]).toMatchObject({ status: 'active' });
  });
});

describe('Issue#268：done 视图停轮询（不砍首挂 GET）', () => {
  it('?view=done 首挂 1 次（status=undefined），advance 900ms×3 计数仍 1（轮询停；修复前涨到 2→3）', async () => {
    vi.useFakeTimers();
    // 关键钉：mock rows 必含 running 态 job——空页下 hasActiveJobs=false 在轮询 gate
    // 首条件短路，M3/M4（gate 移除/恒放行）变异下本用例照绿，杀伤落空（探针 T4 实证）。
    listJobsMock.mockImplementation(async () => jobsPage([runningJob()]));
    renderTasks('/jobs?view=done');
    expect(listJobsMock).toHaveBeenCalledTimes(1);
    expect(listJobsMock.mock.calls[0][0].status).toBeUndefined();

    await vi.advanceTimersByTimeAsync(900);
    await vi.advanceTimersByTimeAsync(900);
    await vi.advanceTimersByTimeAsync(900);
    expect(listJobsMock).toHaveBeenCalledTimes(1);
  });

  it('?view=done 下 jobs 徽标仍呈现首挂数据（防误砍首挂 GET 的回归钉）', async () => {
    listJobsMock.mockImplementation(async () => jobsPage([runningJob()]));
    renderTasks('/jobs?view=done');
    // active 徽标 = jobs.pageMetrics.activeJobs(1) + agentTasks.counts.active(0)；
    // stats 由异步 load 写入，须等 resolve（否则首帧行为断言到 EMPTY stats）。
    await waitFor(() => {
      const badge = screen
        .getByTestId('tasks-tab-active')
        .querySelector('span.tabular-nums');
      expect(badge?.textContent).toBe('1');
    });
  });

  it('对照：/jobs（active）首挂 1 次，advance 后轮询仍在（防过度收紧）', async () => {
    vi.useFakeTimers();
    listJobsMock.mockImplementation(async () => jobsPage([runningJob()]));
    renderTasks('/jobs');
    expect(listJobsMock).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(900);
    expect(listJobsMock.mock.calls.length).toBeGreaterThan(1);
  });
});
