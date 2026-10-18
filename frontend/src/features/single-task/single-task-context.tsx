// Copyright 2026 LexMask Contributors

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react';
import { t } from '@/i18n';
import { notifyDone } from '@/lib/notifications';
import { showToast } from '@/components/Toast';
import type { JobDetail } from '@/services/jobsApi';
import { listActiveSingleJobs } from '@/services/singleTaskApi';
import {
  buildSingleTaskView,
  deriveSingleTaskPhase,
  isTerminalPhase,
  notifyKeyForPhase,
  type SingleTaskPhase,
  type SingleTaskView,
} from './phase';
import { useSingleTaskPoll } from './use-single-task-poll';

export type { SingleTaskPhase, SingleTaskView } from './phase';

/** 完成/失败任务的短留窗口：给角标/提示一拍展示时间，过后从账本清除。 */
export const SINGLE_TASK_DONE_RETAIN_MS = 10_000;

/** 连续整轮轮询全部失败达到该值 → 提示一次离线；任一成功即清零。 */
const OFFLINE_TOAST_AFTER_TICKS = 3;

/**
 * 全局单文件任务账本（Issue#194）：挂载时从 listActiveSingleJobs 恢复、识别页 watch 注册、
 * 轮询推进 phase 并在三类迁移（recognized/redacted/failed）上各发一次通知。
 * T9（进度角标）/T10（提交打码）/T11（离线态）/T12（任务中心入口）都从这里取数。
 */

/** 账本条目：watch 注册 / 恢复注入的任务元数据 + 最近一次观测的 phase（即通知基线）。 */
type LedgerEntry = {
  jobId: string;
  fileId: string;
  filename: string;
  phase: SingleTaskPhase;
};

export type SingleTaskContextValue = {
  tasks: SingleTaskView[];
  taskForFile: (fileId: string) => SingleTaskView | undefined;
  watch: (jobId: string, fileId: string, filename: string) => void;
  refresh: () => Promise<void>;
};

const SingleTaskContext = createContext<SingleTaskContextValue | null>(null);

/** 首轮轮询前的占位视图：让 watch() 注册即刻可被 taskForFile 命中。 */
function placeholderView(entry: LedgerEntry): SingleTaskView {
  return {
    jobId: entry.jobId,
    itemId: '',
    fileId: entry.fileId,
    filename: entry.filename,
    phase: entry.phase,
    current: 0,
    total: 0,
    message: null,
    error: null,
    performance: null,
  };
}

export function SingleTaskProvider({ children }: { children: ReactNode }) {
  const [entries, setEntries] = useState<LedgerEntry[]>([]);
  const [views, setViews] = useState<Record<string, SingleTaskView>>({});
  // entriesRef 是账本真源（watch/applyDetails/removal 都要同步读改），state 只驱动渲染。
  const entriesRef = useRef<LedgerEntry[]>([]);
  const removalTimersRef = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  const failStreakRef = useRef(0);

  const commitEntries = useCallback((next: LedgerEntry[]) => {
    entriesRef.current = next;
    setEntries(next);
  }, []);

  const removeEntry = useCallback(
    (jobId: string) => {
      const pending = removalTimersRef.current.get(jobId);
      if (pending) {
        clearTimeout(pending);
        removalTimersRef.current.delete(jobId);
      }
      commitEntries(entriesRef.current.filter((entry) => entry.jobId !== jobId));
      setViews((prev) => {
        if (!(jobId in prev)) return prev;
        const next = { ...prev };
        delete next[jobId];
        return next;
      });
    },
    [commitEntries],
  );

  const scheduleRemoval = useCallback(
    (jobId: string) => {
      const pending = removalTimersRef.current.get(jobId);
      if (pending) clearTimeout(pending);
      removalTimersRef.current.set(
        jobId,
        setTimeout(() => {
          removalTimersRef.current.delete(jobId);
          removeEntry(jobId);
        }, SINGLE_TASK_DONE_RETAIN_MS),
      );
    },
    [removeEntry],
  );

  /** 识别页提交后注册；对同一 job 幂等（不打断既有通知基线）。 */
  const watch = useCallback(
    (jobId: string, fileId: string, filename: string) => {
      if (entriesRef.current.some((entry) => entry.jobId === jobId)) return;
      const entry: LedgerEntry = { jobId, fileId, filename, phase: 'recognizing' };
      commitEntries([...entriesRef.current, entry]);
      setViews((prev) => ({ ...prev, [jobId]: placeholderView(entry) }));
    },
    [commitEntries],
  );

  // 挂载恢复（D1 刷新存活）：active 单文件任务直接认领；fileId/itemId 待首轮轮询回填。
  useEffect(() => {
    let cancelled = false;
    void listActiveSingleJobs()
      .then((jobs) => {
        if (cancelled || jobs.length === 0) return;
        const known = new Set(entriesRef.current.map((entry) => entry.jobId));
        const additions: LedgerEntry[] = [];
        for (const job of jobs) {
          if (known.has(job.id)) continue;
          additions.push({
            jobId: job.id,
            fileId: '',
            filename: job.title || '',
            // 基线取 summary 状态：恢复本身不触发通知，之后真实迁移才发。
            phase: deriveSingleTaskPhase(job.status, null),
          });
        }
        if (additions.length === 0) return;
        commitEntries([...entriesRef.current, ...additions]);
        setViews((prev) => {
          const next = { ...prev };
          for (const entry of additions) next[entry.jobId] = placeholderView(entry);
          return next;
        });
      })
      .catch(() => {
        /* 列表拉取失败（离线/登出）不阻塞已 watch 任务的轮询 */
      });
    return () => {
      cancelled = true;
    };
  }, [commitEntries]);

  const applyDetails = useCallback(
    (details: JobDetail[]) => {
      if (details.length === 0) return;
      const byId = new Map(details.map((detail) => [detail.id, detail]));
      let entriesChanged = false;
      const transitions: Array<{
        jobId: string;
        to: SingleTaskPhase;
        filename: string;
      }> = [];
      // 先在 ref 上算好下一帧（迁移/回填），再同步 state —— 通知与定时器副作用不进 setState updater。
      const nextEntries = entriesRef.current.map((entry) => {
        const detail = byId.get(entry.jobId);
        if (!detail) return entry;
        const item = detail.items[0];
        const phase = deriveSingleTaskPhase(detail.status, item?.status ?? null);
        const fileId = entry.fileId || item?.file_id || '';
        const filename = entry.filename || detail.title || item?.filename || '';
        if (phase !== entry.phase || fileId !== entry.fileId || filename !== entry.filename) {
          entriesChanged = true;
        }
        if (phase !== entry.phase) {
          transitions.push({ jobId: entry.jobId, to: phase, filename });
        }
        return { ...entry, phase, fileId, filename };
      });
      const detailViews: Record<string, SingleTaskView> = {};
      for (const detail of details) {
        const entry = nextEntries.find((candidate) => candidate.jobId === detail.id);
        if (!entry) continue;
        detailViews[detail.id] = buildSingleTaskView({
          jobId: entry.jobId,
          fileId: entry.fileId,
          filename: entry.filename,
          detail,
        });
      }
      if (entriesChanged) commitEntries(nextEntries);
      setViews((prev) => ({ ...prev, ...detailViews }));
      for (const transition of transitions) {
        const notifyKey = notifyKeyForPhase(transition.to);
        if (notifyKey) {
          notifyDone(t(notifyKey).replace('{name}', transition.filename));
        }
        if (isTerminalPhase(transition.to)) {
          scheduleRemoval(transition.jobId);
        }
      }
    },
    [commitEntries, scheduleRemoval],
  );

  const handlePollSettled = useCallback((allFailed: boolean) => {
    if (!allFailed) {
      failStreakRef.current = 0;
      return;
    }
    failStreakRef.current += 1;
    if (failStreakRef.current === OFFLINE_TOAST_AFTER_TICKS) {
      showToast(t('singleTask.offline'), 'error');
    }
  }, []);

  // 终态任务不再轮询（仍短留在账本里供展示）。
  const watchedJobIds = useMemo(
    () => entries.filter((entry) => !isTerminalPhase(entry.phase)).map((entry) => entry.jobId),
    [entries],
  );

  const { refresh } = useSingleTaskPoll({
    jobIds: watchedJobIds,
    onDetails: applyDetails,
    onPollSettled: handlePollSettled,
  });

  const taskForFile = useCallback(
    (fileId: string) => {
      if (!fileId) return undefined;
      const entry = entriesRef.current.find((candidate) => candidate.fileId === fileId);
      return entry ? views[entry.jobId] : undefined;
    },
    [views],
  );

  const tasks = useMemo(
    () =>
      entries
        .map((entry) => views[entry.jobId])
        .filter((view): view is SingleTaskView => view !== undefined),
    [entries, views],
  );

  const value = useMemo<SingleTaskContextValue>(
    () => ({ tasks, taskForFile, watch, refresh }),
    [tasks, taskForFile, watch, refresh],
  );

  useEffect(
    () => () => {
      for (const timer of removalTimersRef.current.values()) clearTimeout(timer);
      removalTimersRef.current.clear();
    },
    [],
  );

  return <SingleTaskContext.Provider value={value}>{children}</SingleTaskContext.Provider>;
}

/** 全局单文件任务账本（T9-T12 消费）。必须在 <SingleTaskProvider> 内使用。 */
export function useSingleTask(): SingleTaskContextValue {
  const ctx = useContext(SingleTaskContext);
  if (!ctx) throw new Error('useSingleTask must be used within <SingleTaskProvider>');
  return ctx;
}
