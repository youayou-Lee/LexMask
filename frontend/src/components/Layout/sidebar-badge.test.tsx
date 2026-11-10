// Copyright 2026 LexMask Contributors

// Issue#194 Task 12：侧边栏「任务中心」角标（AC2 全局入口可达的计数面）。
// Issue#262：角标语义从 tasks.length 收敛为非终态计数（进行中 + 待复核）——
// done/failed 终态任务留账本短留展示，但不再把用户拽回任务中心。
//
// 钉住的不变量：
// 1) /jobs 导航项按 SingleTaskProvider 账本中非终态（!isTerminalPhase）任务数渲染角标；
// 2) 无单文件任务（或全部处于终态）不渲染角标；
// 3) 识别页 watch 注册即时抬升角标——计数随 provider 变化。
//
// Provider 用真实现（mock 其 API 依赖），AppSidebar 是它的后代（Layout 既有接线）。

// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppSidebar } from './app-sidebar';
import { SidebarProvider } from '@/components/ui/sidebar';
import {
  SingleTaskProvider,
  useSingleTask,
} from '@/features/single-task/single-task-context';
import { getJob, type JobProgress, type JobSummary } from '@/services/jobsApi';
import { listActiveSingleJobs } from '@/services/singleTaskApi';

vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => false }));
vi.mock('@/features/auth/auth-context', () => ({ useAuth: () => ({ status: null }) }));
vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({
    health: null,
    checking: false,
    roundTripMs: null,
    refresh: vi.fn(),
  }),
}));
vi.mock('@/services/singleTaskApi', () => ({ listActiveSingleJobs: vi.fn() }));
vi.mock('@/services/jobsApi', () => ({ getJob: vi.fn() }));
vi.mock('@/lib/notifications', () => ({ notifyDone: vi.fn() }));
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));

const CREATED = '2026-01-01T00:00:00Z';

function activeSingleJob(id: string): JobSummary {
  const progress: JobProgress = {
    total_items: 1,
    pending: 0,
    processing: 1,
    queued: 0,
    parsing: 1,
    ner: 0,
    vision: 0,
    awaiting_review: 0,
    review_approved: 0,
    redacting: 0,
    completed: 0,
    failed: 0,
  };
  return {
    id,
    job_type: 'single',
    title: `${id}.pdf`,
    status: 'processing',
    skip_item_review: false,
    config: { single_file_id: `file-${id}` },
    error_message: null,
    created_at: CREATED,
    updated_at: CREATED,
    progress,
  };
}

/** 识别页提交探针：模拟 Playground 提交成功后的 watch 注册。 */
function WatchProbe({ jobId, fileId, filename }: { jobId: string; fileId: string; filename: string }) {
  const { watch } = useSingleTask();
  useEffect(() => {
    watch(jobId, fileId, filename);
  }, [jobId, fileId, filename, watch]);
  return null;
}

/** 账本探针：把 provider 里的任务 phase 透出来，供「零命中断言先证真识别」用。 */
function LedgerProbe() {
  const { tasks } = useSingleTask();
  return (
    <div data-testid="ledger-probe">
      {tasks.map((task) => (
        <span key={task.jobId} data-testid={`probe-${task.jobId}`} data-phase={task.phase} />
      ))}
    </div>
  );
}

function renderSidebar(children?: React.ReactNode) {
  return render(
    <MemoryRouter>
      <SidebarProvider>
        <SingleTaskProvider>
          {children}
          <AppSidebar />
        </SingleTaskProvider>
      </SidebarProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // getJob 永不落定：测试只验证账本条目数（restore/watch），不驱动轮询状态迁移。
  vi.mocked(getJob).mockImplementation(() => new Promise(() => {}));
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#194 T12：侧边栏任务中心角标', () => {
  it('无单文件任务：/jobs 导航项不渲染角标', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    renderSidebar();
    expect(screen.getByTestId('nav-jobs')).toBeTruthy();
    expect(screen.queryByTestId('nav-badge-jobs')).toBeNull();
  });

  it('挂载恢复的 active（识别中）任务计入角标', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([
      activeSingleJob('j1'),
      activeSingleJob('j2'),
    ]);
    renderSidebar();
    const badge = await screen.findByTestId('nav-badge-jobs');
    expect(badge.textContent).toBe('2');
  });

  it('识别页 watch 注册即时抬升角标（计数随 provider 变化）', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    renderSidebar(<WatchProbe jobId="j9" fileId="file-9" filename="9.pdf" />);
    const badge = await screen.findByTestId('nav-badge-jobs');
    expect(badge.textContent).toBe('1');
  });

  it('终态任务不计入角标：done 任务留账本展示但不渲染角标（Issue#262）', async () => {
    // 挂载恢复基线直接取 summary 状态：status=completed → phase 'done'（终态）。
    vi.mocked(listActiveSingleJobs).mockResolvedValue([
      { ...activeSingleJob('j1'), status: 'completed' },
    ]);
    renderSidebar(<LedgerProbe />);
    // 零命中断言先证「真在账本」：done 任务确实进了 provider，再断言角标不渲染。
    await waitFor(() =>
      expect(screen.getByTestId('probe-j1').getAttribute('data-phase')).toBe('done'),
    );
    expect(screen.queryByTestId('nav-badge-jobs')).toBeNull();
  });
});
