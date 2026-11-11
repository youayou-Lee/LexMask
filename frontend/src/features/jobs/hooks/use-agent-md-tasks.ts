// Copyright 2026 LexMask Contributors

// Issue#282：任务中心「供料任务」面板的数据源。
// 与工单列表并行存在：供料任务是独立对象（agent_md_tasks），按状态并入任务中心，
// 不伪装成 job。轮询节奏沿用任务中心口径：可见 5s、隐藏 30s。

import { useCallback, useEffect, useRef, useState } from 'react';
import {
  agentMdApi,
  type AgentMdTaskCounts,
  type AgentMdTaskSummary,
} from '@/features/agent-md/api';
import { localizeErrorMessage } from '@/utils/localizeError';

const AGENT_MD_TASKS_POLL_MS = 5000;
const AGENT_MD_TASKS_POLL_HIDDEN_MS = 30000;

const EMPTY_COUNTS: AgentMdTaskCounts = { active: 0, awaiting_review: 0 };

export function useAgentMdTasks() {
  const [items, setItems] = useState<AgentMdTaskSummary[]>([]);
  const [counts, setCounts] = useState<AgentMdTaskCounts>(EMPTY_COUNTS);
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState<string | null>(null);
  const seqRef = useRef(0);

  const load = useCallback(async (silent = false) => {
    const seq = ++seqRef.current;
    if (!silent) setLoading(true);
    try {
      const res = await agentMdApi.list();
      if (seq !== seqRef.current) return;
      setItems(res.items);
      setCounts(res.counts);
      setErr(null);
    } catch (error) {
      if (seq !== seqRef.current) return;
      // 面板失败不打断工单账本：保留旧数据，仅提示。
      setErr(localizeErrorMessage(error, 'tasks.agentMd.loadFailed'));
    } finally {
      if (seq === seqRef.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof window.setTimeout> | null = null;
    const isVisible = () =>
      typeof document === 'undefined' || document.visibilityState === 'visible';

    const schedule = () => {
      if (cancelled) return;
      if (timer) clearTimeout(timer);
      timer = window.setTimeout(
        () => {
          if (!cancelled && isVisible()) void load(true);
          schedule();
        },
        isVisible() ? AGENT_MD_TASKS_POLL_MS : AGENT_MD_TASKS_POLL_HIDDEN_MS,
      );
    };

    void load();
    schedule();

    const onVisibility = () => {
      if (isVisible()) void load(true);
      schedule();
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [load]);

  return { items, counts, loading, err, refresh: () => load(true) };
}
