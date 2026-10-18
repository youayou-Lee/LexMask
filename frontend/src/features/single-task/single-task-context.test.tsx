// Copyright 2026 LexMask Contributors

// Issue#194 Task 8：全局 SingleTaskProvider 契约（挂载恢复 / 轮询 / 阶段迁移通知 / taskForFile）。
//
// 钉住的不变量：
// 1) 刷新存活：挂载时 listActiveSingleJobs() 返回的 active 任务直接认领（D1 断点恢复）；
// 2) 三类阶段迁移通知（recognized/redacted/failed）per-job 只发一次（记 lastPhase 基线）；
// 3) watch() 注册即时产生视图（taskForFile 同步命中，不等首轮轮询）；
// 4) done/failed 短留 SINGLE_TASK_DONE_RETAIN_MS 后从账本清除。
//
// 轮询循环的骨架（visibility 门控/in-flight 守卫）抄 jobs 列表轮询
// （features/jobs/hooks/use-jobs.ts:427），本文件用 fake timers 验证节拍与迁移序。
// @vitest-environment jsdom

import { act, cleanup, render } from '@testing-library/react';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { SINGLE_TASK_POLL_MS } from '@/constants/timing';
import { getJob, type JobDetail, type JobSummary } from '@/services/jobsApi';
import { listActiveSingleJobs } from '@/services/singleTaskApi';
import { notifyDone } from '@/lib/notifications';

import {
  SINGLE_TASK_DONE_RETAIN_MS,
  SingleTaskProvider,
  useSingleTask,
  type SingleTaskContextValue,
} from './single-task-context';
import { buildSingleTaskView, deriveSingleTaskPhase, notifyKeyForPhase } from './phase';

vi.mock('@/services/singleTaskApi', () => ({ listActiveSingleJobs: vi.fn() }));
vi.mock('@/services/jobsApi', () => ({ getJob: vi.fn() }));
vi.mock('@/lib/notifications', () => ({ notifyDone: vi.fn() }));
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));

const CREATED = '2026-01-01T00:00:00Z';

function makeSummary(overrides: Partial<JobSummary> = {}): JobSummary {
  return {
    id: 'j1',
    job_type: 'single',
    title: 'a.pdf',
    status: 'processing',
    skip_item_review: false,
    config: {},
    error_message: null,
    created_at: CREATED,
    updated_at: CREATED,
    progress: {
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
    },
    ...overrides,
  };
}

function makeDetail(jobStatus: string, itemStatus: string): JobDetail {
  return {
    ...makeSummary({ status: jobStatus }),
    items: [
      {
        id: 'it1',
        job_id: 'j1',
        file_id: 'f1',
        sort_order: 0,
        status: itemStatus,
        progress_stage: 'parse',
        progress_current: 1,
        progress_total: 3,
        progress_message: '解析第 1/3 页',
        error_message: null,
        created_at: CREATED,
        updated_at: CREATED,
      },
    ],
  };
}

type Ctx = SingleTaskContextValue;

function Probe({ onReady }: { onReady: (ctx: Ctx) => void }) {
  const ctx = useSingleTask();
  useEffect(() => {
    onReady(ctx);
  }, [ctx, onReady]);
  return null;
}

function renderSingleTaskProvider(): {
  handle: { current: Ctx | null };
  unmount: () => void;
} {
  const handle: { current: Ctx | null } = { current: null };
  // 在 React 外创建一次，组件每次渲染拿到的都是同一引用（等效 useCallback）。
  const onReady = (ctx: Ctx) => {
    handle.current = ctx;
  };
  const utils = render(
    <SingleTaskProvider>
      <Probe onReady={onReady} />
    </SingleTaskProvider>,
  );
  return { handle, unmount: () => utils.unmount() };
}

/** 推进 fake 时钟并 flush 轮询 promise / 状态更新。先空转一拍 act：让挂载恢复注册的
 * 轮询定时器在时钟推进**之前**完成调度（advanceTimersByTimeAsync 不回看中途新排的定时器）。 */
async function advance(ms: number): Promise<void> {
  await act(async () => {});
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.clearAllMocks();
});

describe('SingleTaskProvider（Issue#194 Task 8 集成契约）', () => {
  it('(a) 挂载恢复 active 任务并轮询到 awaiting_review：phase 迁移 + notifyDone 恰一次', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([makeSummary({ status: 'processing' })]);
    vi.mocked(getJob).mockResolvedValue(makeDetail('awaiting_review', 'awaiting_review'));

    const { handle } = renderSingleTaskProvider();
    expect(handle.current?.tasks).toHaveLength(0);

    await advance(SINGLE_TASK_POLL_MS * 2);

    const task = handle.current?.tasks[0];
    expect(task).toBeDefined();
    expect(task!.phase).toBe('awaiting_review');
    expect(task!.jobId).toBe('j1');
    expect(task!.itemId).toBe('it1');
    expect(task!.fileId).toBe('f1');
    expect(task!.filename).toBe('a.pdf');
    expect(task!.current).toBe(1);
    expect(task!.total).toBe(3);
    expect(task!.message).toBe('解析第 1/3 页');
    // fileId 首轮轮询回填后 taskForFile 可命中（恢复路径真实可用）
    expect(handle.current!.taskForFile('f1')).toBeDefined();
    expect(notifyDone).toHaveBeenCalledTimes(1);
    expect(notifyDone).toHaveBeenCalledWith('识别完成：a.pdf');
  });

  it('(b) 识别完成→打码入队→完成：迁移通知 recognized/redacted 各一次，failed 不发', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    const seq = [
      makeDetail('processing', 'parsing'),
      makeDetail('awaiting_review', 'awaiting_review'),
      makeDetail('processing', 'review_approved'),
      makeDetail('completed', 'completed'),
    ];
    let call = 0;
    vi.mocked(getJob).mockImplementation(() =>
      Promise.resolve(seq[Math.min(call++, seq.length - 1)] as JobDetail),
    );

    const { handle } = renderSingleTaskProvider();
    await act(async () => {}); // flush 挂载恢复（空列表）
    act(() => {
      handle.current!.watch('j1', 'f1', 'b.pdf');
    });

    const step = () => advance(SINGLE_TASK_POLL_MS);
    await step();
    expect(handle.current!.tasks[0].phase).toBe('recognizing');
    await step();
    expect(handle.current!.tasks[0].phase).toBe('awaiting_review');
    await step();
    expect(handle.current!.tasks[0].phase).toBe('redacting');
    await step();
    expect(handle.current!.tasks[0].phase).toBe('done');

    expect(notifyDone).toHaveBeenCalledTimes(2);
    expect(vi.mocked(notifyDone).mock.calls[0][0]).toBe('识别完成：b.pdf');
    expect(vi.mocked(notifyDone).mock.calls[1][0]).toBe('打码完成：b.pdf');

    // 终态短留后清：账本里不再有 done 任务
    await advance(SINGLE_TASK_DONE_RETAIN_MS);
    expect(handle.current!.tasks).toHaveLength(0);
  });

  it('(c) 刷新后重新挂载：listActiveSingleJobs 返回即恢复（每次挂载各拉一次列表）', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([makeSummary({ status: 'processing' })]);
    vi.mocked(getJob).mockResolvedValue(makeDetail('processing', 'parsing'));

    const first = renderSingleTaskProvider();
    await advance(SINGLE_TASK_POLL_MS);
    expect(first.handle.current?.tasks.map((task) => task.jobId)).toEqual(['j1']);
    first.unmount();

    const second = renderSingleTaskProvider();
    expect(second.handle.current?.tasks).toHaveLength(0);
    await advance(SINGLE_TASK_POLL_MS * 2);
    expect(second.handle.current?.tasks.map((task) => task.jobId)).toEqual(['j1']);
    expect(second.handle.current?.tasks[0].phase).toBe('recognizing');
    expect(listActiveSingleJobs).toHaveBeenCalledTimes(2);
  });

  it('(d) taskForFile 按 fileId 命中 watch 注册的任务（同步、不等首轮轮询）', async () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);

    const { handle } = renderSingleTaskProvider();
    await act(async () => {});
    act(() => {
      handle.current!.watch('j9', 'f9', 'd.pdf');
    });

    const hit = handle.current!.taskForFile('f9');
    expect(hit).toBeDefined();
    expect(hit!.jobId).toBe('j9');
    expect(hit!.filename).toBe('d.pdf');
    expect(hit!.phase).toBe('recognizing');
    expect(handle.current!.taskForFile('missing')).toBeUndefined();
    expect(getJob).not.toHaveBeenCalled();
  });
});

describe('deriveSingleTaskPhase（job/item 状态 → phase 纯映射）', () => {
  it('processing + parsing/ner/vision item ⇒ recognizing', () => {
    expect(deriveSingleTaskPhase('processing', 'parsing')).toBe('recognizing');
    expect(deriveSingleTaskPhase('processing', 'ner')).toBe('recognizing');
    expect(deriveSingleTaskPhase('processing', 'vision')).toBe('recognizing');
  });

  it('processing + review_approved/redacting ⇒ redacting（打码段）', () => {
    expect(deriveSingleTaskPhase('processing', 'review_approved')).toBe('redacting');
    expect(deriveSingleTaskPhase('processing', 'redacting')).toBe('redacting');
  });

  it('item 未知（列表 summary 恢复基线）/ queued ⇒ recognizing', () => {
    expect(deriveSingleTaskPhase('processing', null)).toBe('recognizing');
    expect(deriveSingleTaskPhase('queued', null)).toBe('recognizing');
    expect(deriveSingleTaskPhase('running', 'parsing')).toBe('recognizing');
  });

  it('awaiting_review ⇒ awaiting_review；completed ⇒ done；failed/cancelled ⇒ failed', () => {
    expect(deriveSingleTaskPhase('awaiting_review', 'awaiting_review')).toBe('awaiting_review');
    expect(deriveSingleTaskPhase('completed', 'completed')).toBe('done');
    expect(deriveSingleTaskPhase('failed', 'failed')).toBe('failed');
    expect(deriveSingleTaskPhase('cancelled', 'cancelled')).toBe('failed');
  });
});

describe('notifyKeyForPhase / buildSingleTaskView（纯函数）', () => {
  it('三类通知键一一对应，识别段/打码段不发', () => {
    expect(notifyKeyForPhase('awaiting_review')).toBe('singleTask.notify.recognized');
    expect(notifyKeyForPhase('done')).toBe('singleTask.notify.redacted');
    expect(notifyKeyForPhase('failed')).toBe('singleTask.notify.failed');
    expect(notifyKeyForPhase('recognizing')).toBeNull();
    expect(notifyKeyForPhase('redacting')).toBeNull();
  });

  it('view 从 item 回填 itemId/fileId/进度，空 filename 用 job.title 兜底', () => {
    const view = buildSingleTaskView({
      jobId: 'j1',
      fileId: '',
      filename: '',
      detail: makeDetail('processing', 'parsing'),
    });
    expect(view.itemId).toBe('it1');
    expect(view.fileId).toBe('f1');
    expect(view.filename).toBe('a.pdf');
    expect(view.current).toBe(1);
    expect(view.total).toBe(3);
    expect(view.message).toBe('解析第 1/3 页');
    expect(view.error).toBeNull();
  });

  it('error 取 item.error_message，缺省回落 job.error_message', () => {
    const detail = makeDetail('failed', 'failed');
    detail.error_message = 'job error';
    detail.items[0].error_message = 'item error';
    expect(
      buildSingleTaskView({ jobId: 'j1', fileId: 'f1', filename: 'a.pdf', detail }).error,
    ).toBe('item error');

    const detailNoItemError = makeDetail('failed', 'failed');
    detailNoItemError.error_message = 'job error';
    detailNoItemError.items[0].error_message = null;
    expect(
      buildSingleTaskView({
        jobId: 'j1',
        fileId: 'f1',
        filename: 'a.pdf',
        detail: detailNoItemError,
      }).error,
    ).toBe('job error');
  });
});
