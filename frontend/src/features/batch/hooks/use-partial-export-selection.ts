// Copyright 2026 LexMask Contributors

// Issue#257 需求重对齐（2026-11-11）：部分导出的选择/提交/记录逻辑。
//
// 两个入口共用这一份逻辑：
//  - 批量识别步：文件行展开的页格 / 部分导出对话框；
//  - 单文件任务（/single）：识别中浮层与结果页的「逐页结果与选页导出」块。
// 语义要点（用户口径）：
//  - **不再有「我已了解失败页」勾选**：区间内的失败页由后端自动排除，说明页如实列出；
//  - 失败页/未跑页在页格上不可选，且已选页一旦变成失败会立刻从选择集里摘掉；
//  - 识别进行中也允许导出，产物是那一刻的快照（说明页带 snapshot_at）。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { ApiError } from '@/services/api-client';
import {
  getPartialExport,
  listItemExports,
  startPartialExport,
  type ItemPageStatus,
  type PartialExportTask,
} from '@/services/jobsApi';

import { useBatchPageStatus } from './use-batch-page-status';

const TASK_POLL_MS = 1500;
const RUNNING_STATUSES: ReadonlySet<string> = new Set(['queued', 'running']);

interface UsePartialExportSelectionArgs {
  jobId: string | null;
  itemId: string | null;
  enabled?: boolean;
  /** 上游带下来的预选页号；与「当前成功页」取交集，空则默认全选成功页。 */
  initialSelected?: readonly number[] | null;
}

export interface PartialExportSelection {
  status: ItemPageStatus | null;
  statusError: string | null;
  refreshStatus: () => void;
  selected: ReadonlySet<number>;
  setSelected: (next: Set<number>) => void;
  orderedSelected: number[];
  failedPages: number[];
  excludedInSpan: number[];
  records: PartialExportTask[];
  recordsError: string | null;
  activeTask: PartialExportTask | null;
  submitting: boolean;
  actionError: string | null;
  submit: () => Promise<void>;
}

export function usePartialExportSelection({
  jobId,
  itemId,
  enabled = true,
  initialSelected = null,
}: UsePartialExportSelectionArgs): PartialExportSelection {
  const t = useT();
  const { status, error: statusError, refresh: refreshStatus } = useBatchPageStatus({
    jobId,
    itemId,
    enabled,
  });
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<PartialExportTask | null>(null);
  const [records, setRecords] = useState<PartialExportTask[]>([]);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  // 每个 (job,item) 只初始化一次选择；轮询回来不覆盖用户的选择。
  const initializedForRef = useRef<string | null>(null);
  const key = jobId && itemId ? `${jobId}:${itemId}` : null;

  const pages = useMemo(() => status?.pages ?? [], [status]);
  const successKey = useMemo(
    () =>
      pages
        .filter((cell) => cell.state === 'success')
        .map((cell) => cell.page)
        .join(','),
    [pages],
  );

  useEffect(() => {
    if (!enabled) {
      initializedForRef.current = null;
      setSelected(new Set());
      setActionError(null);
      setActiveTask(null);
      return;
    }
    if (!status || initializedForRef.current === key) return;
    initializedForRef.current = key;
    const successPages = pages
      .filter((cell) => cell.state === 'success')
      .map((cell) => cell.page);
    const successSet = new Set(successPages);
    const prefill = (initialSelected ?? []).filter((page) => successSet.has(page));
    setSelected(new Set(prefill.length > 0 ? prefill : successPages));
  }, [enabled, initialSelected, key, pages, status]);

  // 选择集随页状态剪枝（运行中某页转 failed 后不得留在选择里）。
  useEffect(() => {
    const allowed = new Set(successKey ? successKey.split(',').map(Number) : []);
    setSelected((prev) => {
      if (prev.size === 0) return prev;
      let changed = false;
      const next = new Set<number>();
      prev.forEach((page) => {
        if (allowed.has(page)) next.add(page);
        else changed = true;
      });
      return changed ? next : prev;
    });
  }, [successKey]);

  const loadRecords = useCallback(async () => {
    if (!jobId || !itemId) return;
    try {
      const payload = await listItemExports(jobId, itemId);
      setRecords(payload.exports);
      setRecordsError(
        payload.unavailable.length > 0
          ? t('batchWizard.partialExport.recordsUnavailable').replace(
              '{count}',
              String(payload.unavailable.length),
            )
          : null,
      );
    } catch (err) {
      setRecordsError(err instanceof Error ? err.message : String(err));
    }
  }, [itemId, jobId, t]);

  useEffect(() => {
    if (!enabled) return;
    void loadRecords();
  }, [enabled, loadRecords]);

  useEffect(() => {
    if (!activeTask || !RUNNING_STATUSES.has(activeTask.status)) return;
    const timer = setTimeout(() => {
      void getPartialExport(activeTask.export_id)
        .then((next) => {
          setActiveTask(next);
          if (!RUNNING_STATUSES.has(next.status)) void loadRecords();
        })
        .catch(() => {
          /* 单次轮询失败不打断：状态仍是 running 会继续下一拍 */
        });
    }, TASK_POLL_MS);
    return () => clearTimeout(timer);
  }, [activeTask, loadRecords]);

  const orderedSelected = useMemo(() => [...selected].sort((a, b) => a - b), [selected]);
  const failedPages = useMemo(
    () => pages.filter((cell) => cell.state === 'failed').map((cell) => cell.page),
    [pages],
  );
  const excludedInSpan = useMemo(() => {
    if (orderedSelected.length === 0) return [];
    const min = orderedSelected[0];
    const max = orderedSelected[orderedSelected.length - 1];
    return failedPages.filter((page) => page >= min && page <= max);
  }, [failedPages, orderedSelected]);

  const submit = useCallback(async () => {
    if (!jobId || !itemId || orderedSelected.length === 0 || submitting) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const task = await startPartialExport(jobId, itemId, orderedSelected);
      setActiveTask(task);
      void loadRecords();
    } catch (err) {
      if (err instanceof ApiError) {
        const code = err.errorCode ?? '';
        setActionError(
          code
            ? t('batchWizard.partialExport.errorWithCode')
                .replace('{code}', code)
                .replace('{message}', err.message)
            : err.message,
        );
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }, [itemId, jobId, loadRecords, orderedSelected, submitting, t]);

  return {
    status,
    statusError,
    refreshStatus,
    selected,
    setSelected,
    orderedSelected,
    failedPages,
    excludedInSpan,
    records,
    recordsError,
    activeTask,
    submitting,
    actionError,
    submit,
  };
}