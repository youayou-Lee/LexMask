// Copyright 2026 LexMask Contributors

// Issue#278 统一导出（原 Issue#257 use-partial-export-selection 改名+改造）：
// 完整导出与选页导出共用这一份选择/提交/记录逻辑。
//
// 语义要点：
//  - kind='partial' 发 orderedSelected；kind='full' 不发 pages（缺省=全部可导出页）；
//  - 失败页/未跑页在页格上不可选，且已选页一旦变成失败会立刻从选择集里摘掉；
//  - 「回到编辑保留选择集」：弹窗关闭不清空选择，写入模块级 cache（键
//    `${jobId}:${itemId}`），重开时读 cache 预填再与最新页状态交集剪枝——
//    会话内存活、刷新即失（issue 只要求「改完回来保留」）；
//  - 剪枝谓词 = success ∪ manual_verified（r2 F4：success-only 会把 manual 页
//    静默摘出选择集，与「manual 页默认包含」裁定冲突；#280 合入后由其四态
//    normalizer 生产 manual_verified，本支防御性接受）。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { ApiError } from '@/services/api-client';
import {
  getItemExport,
  listItemExports,
  startItemExport,
  type ItemExportTask,
  type ItemPageStatus,
} from '@/services/jobsApi';

import { useBatchPageStatus } from './use-batch-page-status';
// NOTE: batch 钩子复用 playground 的 BoundingBox 类型（历史形状同源）；若后续拆分
// 共享类型模块，这里改为从 shared 引入，避免 batch→playground 反向依赖。
import type { BoundingBox } from '@/features/playground/types';

const TASK_POLL_MS = 1500;
const RUNNING_STATUSES: ReadonlySet<string> = new Set(['queued', 'running']);

/** 「回到编辑保留选择集」的存储层：模块级 Map，会话内存活（§3.3）。 */
const selectionCache = new Map<string, Set<number>>();

/** exportable 谓词（r2 F4）：success ∪ manual_verified。 */
function isExportableState(state: string): boolean {
  return state === 'success' || state === 'manual_verified';
}

export type ItemExportKind = 'full' | 'partial';

interface UseItemExportArgs {
  jobId: string | null;
  itemId: string | null;
  enabled?: boolean;
  kind?: ItemExportKind;
  /** 上游带下来的预选页号；与「当前可导出页」取交集，空则默认全选可导出页。 */
  initialSelected?: readonly number[] | null;
  /** Issue#322：当前画布框快照（含手动拉框），partial 导出时随请求提交。 */
  boundingBoxes?: readonly BoundingBox[] | null;
}

export interface ItemExportSelection {
  status: ItemPageStatus | null;
  statusError: string | null;
  refreshStatus: () => void;
  kind: ItemExportKind;
  setKind: (kind: ItemExportKind) => void;
  includeSummary: boolean;
  setIncludeSummary: (value: boolean) => void;
  selected: ReadonlySet<number>;
  setSelected: (next: Set<number>) => void;
  orderedSelected: number[];
  failedPages: number[];
  excludedInSpan: number[];
  records: ItemExportTask[];
  recordsError: string | null;
  activeTask: ItemExportTask | null;
  submitting: boolean;
  actionError: string | null;
  submit: () => Promise<void>;
}

export function useItemExport({
  jobId,
  itemId,
  enabled = true,
  kind: initialKind = 'partial',
  initialSelected = null,
  boundingBoxes = null,
}: UseItemExportArgs): ItemExportSelection {
  const t = useT();
  const { status, error: statusError, refresh: refreshStatus } = useBatchPageStatus({
    jobId,
    itemId,
    enabled,
  });
  const [kind, setKind] = useState<ItemExportKind>(initialKind);
  const [includeSummary, setIncludeSummary] = useState(false);
  const [selected, setSelected] = useState<Set<number>>(() => new Set());
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<ItemExportTask | null>(null);
  const [records, setRecords] = useState<ItemExportTask[]>([]);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  // 每个 (job,item) 只初始化一次选择；轮询回来不覆盖用户的选择。
  const initializedForRef = useRef<string | null>(null);
  const key = jobId && itemId ? `${jobId}:${itemId}` : null;

  // Issue#313 门⑥ I-1：playground 宿主的 `enabled` 在挂载期恒为 true（`Boolean(itemId)`），
  // Dialog 关闭是整棵子树卸载，走不到下面的 !enabled 分支 ⇒ 必须在卸载时把最新选择集
  // 写回 cache，否则「回到编辑 → 重开导出」会回落成全选而不是用户选的那几页。
  const selectedRef = useRef(selected);
  selectedRef.current = selected;

  const pages = useMemo(() => status?.pages ?? [], [status]);
  // Issue#280 × #278 r2 F4：manual_verified 页与 success 页同口径进选择集
  // （exportable 谓词；后端只挡 failed——manual 页已物理移出失败清单）。
  const exportableKey = useMemo(
    () =>
      pages
        .filter((cell) => isExportableState(cell.state))
        .map((cell) => cell.page)
        .join(','),
    [pages],
  );

  // 卸载 / 切换 (job,item) 时落 cache；覆盖 playground 宿主「关闭弹窗 = 整棵子树卸载」的路径。
  useEffect(() => {
    if (!key) return undefined;
    return () => {
      selectionCache.set(key, selectedRef.current);
    };
  }, [key]);

  useEffect(() => {
    if (!enabled) {
      // 关弹窗不清空选择：写 cache 供「回到编辑 → 回到导出」恢复（§3.3）。
      if (key) selectionCache.set(key, selectedRef.current);
      initializedForRef.current = null;
      setActionError(null);
      setActiveTask(null);
      return;
    }
    if (!status || initializedForRef.current === key) return;
    initializedForRef.current = key;
    const exportablePages = pages
      .filter((cell) => isExportableState(cell.state))
      .map((cell) => cell.page);
    const exportableSet = new Set(exportablePages);
    // 重开时读 cache 预填，再与最新页状态交集剪枝；cache 无条目 → 缺省全选 exportable。
    const cached = key ? selectionCache.get(key) : undefined;
    const prefillFromCache = cached
      ? [...cached].filter((page) => exportableSet.has(page))
      : [];
    const prefillFromProps = (initialSelected ?? []).filter((page) => exportableSet.has(page));
    const prefill = prefillFromCache.length > 0 ? prefillFromCache : prefillFromProps;
    setSelected(new Set(prefill.length > 0 ? prefill : exportablePages));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 初始化只跑一次（key 变化重置）
  }, [enabled, key, pages, status]);

  // 选择集随页状态剪枝（运行中某页转 failed 后不得留在选择里；谓词 r2 F4）。
  useEffect(() => {
    const allowed = new Set(exportableKey ? exportableKey.split(',').map(Number) : []);
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
  }, [exportableKey]);

  const loadRecords = useCallback(async () => {
    if (!jobId || !itemId) return;
    try {
      const payload = await listItemExports(jobId, itemId);
      setRecords(payload.exports);
      setRecordsError(
        payload.unavailable.length > 0
          ? t('batchWizard.export.recordsUnavailable').replace(
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
      void getItemExport(activeTask.export_id)
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
    if (!jobId || !itemId || submitting || (kind === 'partial' && orderedSelected.length === 0)) {
      return;
    }
    setSubmitting(true);
    setActionError(null);
    try {
      const task = await startItemExport(jobId, itemId, {
        kind,
        pages: kind === 'partial' ? orderedSelected : undefined,
        includeSummary,
        boundingBoxes: boundingBoxes ? [...boundingBoxes] : undefined,
      });
      setActiveTask(task);
      void loadRecords();
    } catch (err) {
      if (err instanceof ApiError) {
        // 已知错误码走专门文案；未知码回落通用（retryErrorMessage 同款模式）。
        const code = err.errorCode ?? '';
        const specificKey = `batchWizard.export.error.${code || 'unknown'}`;
        const specific = code ? t(specificKey) : '';
        if (code && specific !== specificKey) {
          setActionError(specific.replace('{message}', err.message));
        } else {
          setActionError(
            code
              ? t('batchWizard.export.errorWithCode')
                  .replace('{code}', code)
                  .replace('{message}', err.message)
              : err.message,
          );
        }
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  }, [itemId, jobId, loadRecords, orderedSelected, submitting, t, kind, includeSummary, boundingBoxes]);

  return {
    status,
    statusError,
    refreshStatus,
    kind,
    setKind,
    includeSummary,
    setIncludeSummary,
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
