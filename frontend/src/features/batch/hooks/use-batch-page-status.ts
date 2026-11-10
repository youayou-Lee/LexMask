// Copyright 2026 LexMask Contributors

// Issue#257 一期：页级状态轮询（页格三态 + 页选器共用）。
//
// 设计取舍：
//  - 识别进行中每 3s 拉一次；`recognition_state` 落到终态（done/failed/cancelled）
//    或组件卸载即停——不做全 job 详情轮询（万级任务详情可达几十 MB）。
//  - 端点自身是快照（snapshot_at），失败页清单是 reason 的权威来源，前端不重算。
//  - 404（文件记录缺失）+ 非分页类型都当作「没有页级视图」，不向上抛错。

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError } from '@/services/api-client';
import { getItemPageStatus, type ItemPageStatus } from '@/services/jobsApi';

export const PAGE_STATUS_POLL_MS = 3000;

const TERMINAL_STATES: ReadonlySet<string> = new Set(['done', 'failed', 'cancelled']);

export type UseBatchPageStatusResult = {
  status: ItemPageStatus | null;
  error: string | null;
  loading: boolean;
  refresh: () => Promise<void>;
};

export function useBatchPageStatus(params: {
  jobId?: string | null;
  itemId?: string | null;
  enabled?: boolean;
}): UseBatchPageStatusResult {
  const { jobId, itemId, enabled = true } = params;
  const [status, setStatus] = useState<ItemPageStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const mountedRef = useRef(true);

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  const fetchOnce = useCallback(async (): Promise<ItemPageStatus | null> => {
    if (!jobId || !itemId) return null;
    try {
      return await getItemPageStatus(jobId, itemId);
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  }, [jobId, itemId]);

  const refresh = useCallback(async () => {
    if (!jobId || !itemId) return;
    setLoading(true);
    try {
      const next = await fetchOnce();
      if (!mountedRef.current) return;
      setStatus(next);
      setError(null);
    } catch (err) {
      if (!mountedRef.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (mountedRef.current) setLoading(false);
    }
  }, [fetchOnce, itemId, jobId]);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      clearTimer();
    };
  }, [clearTimer]);

  useEffect(() => {
    clearTimer();
    if (!enabled || !jobId || !itemId) {
      if (!enabled) setStatus(null);
      return;
    }
    let stopped = false;

    const tick = async () => {
      setLoading(true);
      let next: ItemPageStatus | null = null;
      let failed = false;
      try {
        next = await fetchOnce();
      } catch (err) {
        failed = true;
        if (!stopped) setError(err instanceof Error ? err.message : String(err));
      } finally {
        if (!stopped) setLoading(false);
      }
      if (stopped) return;
      if (!failed) {
        setStatus(next);
        setError(null);
      }
      // 状态还在涨就继续轮询；终态（或取不到）停手，避免后台空转。
      if (next && !TERMINAL_STATES.has(next.recognition_state)) {
        timerRef.current = setTimeout(() => void tick(), PAGE_STATUS_POLL_MS);
      }
    };

    void tick();
    return () => {
      stopped = true;
      clearTimer();
    };
  }, [clearTimer, enabled, fetchOnce, itemId, jobId]);

  return { status, error, loading, refresh };
}