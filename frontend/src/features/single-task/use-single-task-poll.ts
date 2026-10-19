// Copyright 2026 LexMask Contributors

import { useCallback, useEffect, useRef } from 'react';
import { getJob, type JobDetail } from '@/services/jobsApi';
import { SINGLE_TASK_POLL_HIDDEN_MS, SINGLE_TASK_POLL_MS } from '@/constants/timing';

/**
 * 单文件任务轮询循环（Issue#194）：自重排 setTimeout + visibility 门控 + in-flight 守卫，
 * 行为骨架与 jobs 列表轮询（features/jobs/hooks/use-jobs.ts）一致：
 * - 页签隐藏时不发请求，下一拍放宽为 SINGLE_TASK_POLL_HIDDEN_MS（30s）；
 * - visibilitychange 回到前台立即补一轮；
 * - jobIds 变化（watch 注册 / 终态摘除）时重建循环；
 * - 每拍对全部 watched job 并发 getJob(id, { performance: true })，
 *   单个失败不影响其余（该拍全部失败才记 allFailed，供离线提示计数）。
 */

export type SingleTaskPollHandlers = {
  jobIds: string[];
  onDetails: (details: JobDetail[]) => void;
  /** 一轮结束回调：allFailed=true 表示本轮所有 job 拉取均失败。 */
  onPollSettled?: (allFailed: boolean) => void;
};

export function useSingleTaskPoll({
  jobIds,
  onDetails,
  onPollSettled,
}: SingleTaskPollHandlers): { refresh: () => Promise<void> } {
  const onDetailsRef = useRef(onDetails);
  const onPollSettledRef = useRef(onPollSettled);
  useEffect(() => {
    onDetailsRef.current = onDetails;
    onPollSettledRef.current = onPollSettled;
  });

  const pollNowRef = useRef<(() => Promise<void>) | null>(null);

  const idsKey = jobIds.join('\u001f');

  useEffect(() => {
    const ids = idsKey ? idsKey.split('\u001f') : [];
    if (ids.length === 0) {
      pollNowRef.current = null;
      return;
    }
    let cancelled = false;
    let inFlight = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let lastTick: Promise<void> = Promise.resolve();

    const clearPollTimer = () => {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
    };

    const tick = (): Promise<void> => {
      if (inFlight) return lastTick;
      inFlight = true;
      lastTick = (async () => {
        try {
          const settled = await Promise.all(
            ids.map((id) =>
              getJob(id, { performance: true }).then(
                (detail) => ({ ok: true as const, detail }),
                () => ({ ok: false as const }),
              ),
            ),
          );
          if (cancelled) return;
          const details = settled
            .filter((entry): entry is { ok: true; detail: JobDetail } => entry.ok)
            .map((entry) => entry.detail);
          onDetailsRef.current(details);
          onPollSettledRef.current?.(details.length === 0);
        } finally {
          inFlight = false;
        }
      })();
      return lastTick;
    };

    const scheduleNextPoll = () => {
      if (cancelled) return;
      clearPollTimer();
      const hidden = typeof document !== 'undefined' && document.visibilityState !== 'visible';
      timer = setTimeout(
        () => {
          void poll();
        },
        hidden ? SINGLE_TASK_POLL_HIDDEN_MS : SINGLE_TASK_POLL_MS,
      );
    };

    const poll = async (): Promise<void> => {
      if (cancelled || inFlight) return;
      if (typeof document !== 'undefined' && document.visibilityState !== 'visible') {
        scheduleNextPoll();
        return;
      }
      try {
        await tick();
      } finally {
        scheduleNextPoll();
      }
    };

    const handleVisibilityChange = () => {
      clearPollTimer();
      if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
        void poll();
      } else {
        scheduleNextPoll();
      }
    };
    if (typeof document !== 'undefined') {
      document.addEventListener('visibilitychange', handleVisibilityChange);
    }
    scheduleNextPoll();

    // 手动刷新（refresh()）：不做 visibility 门控、in-flight 时复用当轮 promise。
    pollNowRef.current = () => tick();

    return () => {
      cancelled = true;
      clearPollTimer();
      pollNowRef.current = null;
      if (typeof document !== 'undefined') {
        document.removeEventListener('visibilitychange', handleVisibilityChange);
      }
    };
  }, [idsKey]);

  const refresh = useCallback(() => pollNowRef.current?.() ?? Promise.resolve(), []);

  return { refresh };
}
