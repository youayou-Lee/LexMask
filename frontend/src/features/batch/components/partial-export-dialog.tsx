// Copyright 2026 LexMask Contributors

// Issue#257 一期：部分导出对话框（页选器 + 说明页知情确认 + 导出记录）。
//
// 与后端契约（§四）：
//  - 失败页永不进包；所选页区间内含失败页时必须显式确认 → 409 failed_pages_ack_required。
//  - 202 受理后是异步任务；状态与下载复用 /files/batch/export/{export_id}(/volumes/{name})。
//  - 记录跨重启仍在（后端扫 export-status.json），所以每次打开都重新拉一遍列表。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { ApiError } from '@/services/api-client';
import { batchExportApi } from '@/services/api';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

import {
  getPartialExport,
  listItemExports,
  startPartialExport,
  type ItemPageStatus,
  type PartialExportTask,
} from '@/services/jobsApi';
import { useBatchPageStatus } from '../hooks/use-batch-page-status';
import { BatchPageMatrix } from './batch-page-matrix';

interface PartialExportDialogProps {
  open: boolean;
  jobId: string | null;
  itemId: string | null;
  filename: string;
  /** 行内页选器带上来的页号；为空则默认全选成功页。 */
  initialSelected?: number[] | null;
  onClose: () => void;
}

const TASK_POLL_MS = 1500;
const RUNNING_STATUSES: ReadonlySet<string> = new Set(['queued', 'running']);

// 动态拼 key 会在缺键时把 key 原样吐到界面（i18n 的兜底就是返回 key），
// 所以状态文案走显式映射；未知状态直出原始字符串。
function taskStatusKey(status: string): string {
  switch (status) {
    case 'queued':
      return 'batchWizard.partialExport.taskStatus.queued';
    case 'running':
      return 'batchWizard.partialExport.taskStatus.running';
    case 'completed':
      return 'batchWizard.partialExport.taskStatus.completed';
    case 'failed':
      return 'batchWizard.partialExport.taskStatus.failed';
    default:
      return '';
  }
}

export function PartialExportDialog({
  open,
  jobId,
  itemId,
  filename,
  initialSelected = null,
  onClose,
}: PartialExportDialogProps) {
  const t = useT();
  const { status, error, refresh } = useBatchPageStatus({ jobId, itemId, enabled: open });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [ack, setAck] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [activeTask, setActiveTask] = useState<PartialExportTask | null>(null);
  const [records, setRecords] = useState<PartialExportTask[]>([]);
  const [recordsError, setRecordsError] = useState<string | null>(null);
  // 每次打开/换文件都重新初始化选择，但轮询回来不覆盖用户的选择。
  const initializedForRef = useRef<string | null>(null);

  const key = jobId && itemId ? `${jobId}:${itemId}` : null;

  useEffect(() => {
    if (!open) {
      initializedForRef.current = null;
      setSelected(new Set());
      setAck(false);
      setActionError(null);
      setActiveTask(null);
      return;
    }
    if (!status || initializedForRef.current === key) return;
    initializedForRef.current = key;
    const successPages = status.pages.filter((cell) => cell.state === 'success').map((c) => c.page);
    const successSet = new Set(successPages);
    const prefill = (initialSelected ?? []).filter((page) => successSet.has(page));
    setSelected(new Set(prefill.length > 0 ? prefill : successPages));
  }, [key, open, status]);

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
    if (!open) return;
    void loadRecords();
  }, [loadRecords, open]);

  // 异步导出任务：轮询到终态再刷新记录（下载链接只在 completed 后出现）。
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

  const pages = status?.pages ?? [];
  const failedPages = useMemo(
    () => pages.filter((cell) => cell.state === 'failed').map((cell) => cell.page),
    [pages],
  );
  const orderedSelected = useMemo(() => [...selected].sort((a, b) => a - b), [selected]);
  const excludedInSpan = useMemo(() => {
    if (orderedSelected.length === 0) return [];
    const min = orderedSelected[0];
    const max = orderedSelected[orderedSelected.length - 1];
    return failedPages.filter((page) => page >= min && page <= max);
  }, [failedPages, orderedSelected]);

  // 页状态会随轮询变化：已选的页转成 failed／未跑时必须从选择集里摘掉，
  // 否则用户点不掉它、提交会被后端拒。
  const successKey = useMemo(
    () =>
      pages
        .filter((cell) => cell.state === 'success')
        .map((cell) => cell.page)
        .join(','),
    [pages],
  );
  useEffect(() => {
    const allowed = new Set(
      successKey ? successKey.split(',').map((value) => Number(value)) : [],
    );
    if (allowed.size === 0) return;
    setSelected((prev) => {
      const next = new Set([...prev].filter((page) => allowed.has(page)));
      return next.size === prev.size ? prev : next;
    });
  }, [successKey]);

  // 确认只对「当次选择」有效：选择一变就必须重新勾选（评审 Minor）。
  const ackKey = `${orderedSelected.join(',')}|${excludedInSpan.join(',')}`;
  useEffect(() => {
    setAck(false);
  }, [ackKey]);

  const handleExport = async () => {
    if (!jobId || !itemId || orderedSelected.length === 0 || submitting) return;
    setSubmitting(true);
    setActionError(null);
    try {
      const task = await startPartialExport(jobId, itemId, orderedSelected, ack);
      setActiveTask(task);
      void loadRecords();
    } catch (err) {
      if (err instanceof ApiError) {
        const code = err.errorCode ?? '';
        if (code === 'failed_pages_ack_required') {
          setActionError(t('batchWizard.partialExport.errorAckRequired'));
        } else if (code) {
          setActionError(
            t('batchWizard.partialExport.errorWithCode')
              .replace('{code}', code)
              .replace('{message}', err.message),
          );
        } else {
          setActionError(err.message);
        }
      } else {
        setActionError(err instanceof Error ? err.message : String(err));
      }
    } finally {
      setSubmitting(false);
    }
  };

  const paged = status?.paged ?? false;
  const recognitionState = status?.recognition_state ?? 'not_started';
  // 识别进行中也允许导出（Issue#257 的核心诉求：跑到 100 页就能先导前 100 页）；
  // 导出侧另取一次快照，产物带 snapshot_at 说明它只是那一刻的结果。
  const canPick = paged; // 文本类型由后端 400 兜底，前端只在 paged 上放开入口

  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      {open ? (
        <DialogContent
          className="max-h-[86vh] max-w-3xl gap-0 overflow-hidden border-border/70 bg-[var(--surface-overlay)] p-0"
          data-testid="partial-export-dialog"
        >
          <DialogHeader className="border-b border-border/70 px-5 pb-3 pt-5">
            <DialogTitle>{t('batchWizard.partialExport.title')}</DialogTitle>
            <DialogDescription className="truncate text-sm leading-relaxed">
              <span className="font-medium text-foreground">{filename}</span>
            </DialogDescription>
          </DialogHeader>

          <div className="flex min-h-0 flex-col gap-3 overflow-y-auto px-5 py-4">
            {error && (
              <p className="text-xs text-destructive" data-testid="partial-export-status-error">
                {error}
              </p>
            )}

            {!status && !error && (
              <p className="text-xs text-muted-foreground">{t('batchWizard.partialExport.loading')}</p>
            )}

            {status && !paged && (
              <p className="text-xs text-muted-foreground" data-testid="partial-export-unsupported">
                {t('batchWizard.partialExport.unsupportedType')}
              </p>
            )}

            {status && paged && (
              <>
                {recognitionState === 'running' && (
                  <p className="text-xs text-muted-foreground" data-testid="partial-export-running">
                    {t('batchWizard.partialExport.runningHint')}
                  </p>
                )}
                <BatchPageMatrix
                  status={status as ItemPageStatus}
                  selected={selected}
                  onChange={setSelected}
                  disabled={!canPick}
                />

                {excludedInSpan.length > 0 && (
                  <label className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={ack}
                      onChange={(event) => setAck(event.target.checked)}
                      data-testid="partial-export-ack"
                    />
                    <span className="text-destructive">
                      {t('batchWizard.partialExport.ackFailed')
                        .replace('{pages}', excludedInSpan.join('、'))
                        .replace('{count}', String(excludedInSpan.length))}
                    </span>
                  </label>
                )}

                <p className="text-xs text-muted-foreground">
                  {t('batchWizard.partialExport.coverHint')}
                </p>

                {actionError && (
                  <p className="text-xs text-destructive" data-testid="partial-export-error">
                    {actionError}
                  </p>
                )}

                {activeTask && (
                  <div
                    className="rounded-lg border border-border/70 bg-background px-3 py-2 text-xs"
                    data-testid="partial-export-task"
                  >
                    <div className="flex flex-wrap items-center gap-2">
                      <span className="font-medium">{activeTask.title || activeTask.export_id}</span>
                      <span className="text-muted-foreground">
                        {taskStatusKey(activeTask.status)
                          ? t(taskStatusKey(activeTask.status))
                          : activeTask.status}
                      </span>
                    </div>
                    {activeTask.status === 'completed' && (
                      <div className="mt-1.5 flex flex-wrap items-center gap-2">
                        {activeTask.volumes.map((volume) => (
                          <a
                            key={volume.name}
                            href={batchExportApi.volumeUrl(activeTask.export_id, volume.name)}
                            className="font-medium text-primary underline underline-offset-2"
                            download
                            data-testid={`partial-export-download-${volume.name}`}
                          >
                            {t('batchWizard.partialExport.download')}
                          </a>
                        ))}
                      </div>
                    )}
                    {activeTask.status === 'failed' && (
                      <p className="mt-1 text-destructive">{activeTask.error ?? ''}</p>
                    )}
                  </div>
                )}

                <div className="flex flex-wrap items-center justify-end gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    className="h-9"
                    onClick={() => {
                      void refresh();
                      void loadRecords();
                    }}
                    disabled={submitting}
                    data-testid="partial-export-refresh"
                  >
                    {t('batchWizard.partialExport.refresh')}
                  </Button>
                  <Button
                    type="button"
                    className="h-9"
                    onClick={() => void handleExport()}
                    disabled={
                      submitting ||
                      !canPick ||
                      orderedSelected.length === 0 ||
                      (excludedInSpan.length > 0 && !ack)
                    }
                    data-testid="partial-export-submit"
                  >
                    {submitting
                      ? t('batchWizard.partialExport.submitting')
                      : t('batchWizard.partialExport.submit').replace(
                          '{count}',
                          String(orderedSelected.length),
                        )}
                  </Button>
                </div>
              </>
            )}

            <section className="border-t border-border/70 pt-3" data-testid="partial-export-records">
              <h3 className="text-xs font-medium">{t('batchWizard.partialExport.recordsTitle')}</h3>
              {recordsError && <p className="mt-1 text-xs text-destructive">{recordsError}</p>}
              {records.length === 0 ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  {t('batchWizard.partialExport.recordsEmpty')}
                </p>
              ) : (
                <ul className="mt-1.5 flex flex-col gap-1.5">
                  {records.map((record) => (
                    <li
                      key={record.export_id}
                      className="flex flex-wrap items-center gap-2 rounded-md border border-border/60 px-2.5 py-1.5 text-xs"
                      data-testid={`partial-export-record-${record.export_id}`}
                    >
                      <span className="min-w-0 flex-1 truncate font-medium" title={record.title}>
                        {record.title || record.export_id}
                      </span>
                      {record.page_count && record.pages?.length ? (
                        <span
                          className="shrink-0 text-muted-foreground tabular-nums"
                          data-testid={`partial-export-record-meta-${record.export_id}`}
                        >
                          {t('batchWizard.partialExport.recordSummary')
                            // 产物 = 选中页 + 说明页 1 页，摘要必须报包内页数而不是选中页数
                            .replace('{pages}', String(record.pages.length + 1))
                            .replace('{total}', String(record.page_count))
                            .replace('{excluded}', String(record.excluded_failed_pages?.length ?? 0))}
                        </span>
                      ) : null}
                      <span className="shrink-0 text-muted-foreground tabular-nums">
                        {new Date(record.created_at).toLocaleString()}
                      </span>
                      {record.status === 'completed' ? (
                        record.volumes.map((volume) => (
                          <a
                            key={volume.name}
                            href={batchExportApi.volumeUrl(record.export_id, volume.name)}
                            className="shrink-0 text-primary underline underline-offset-2"
                            download
                          >
                            {t('batchWizard.partialExport.download')}
                          </a>
                        ))
                      ) : (
                        <span className="shrink-0 text-muted-foreground">
                          {taskStatusKey(record.status)
                            ? t(taskStatusKey(record.status))
                            : record.status}
                        </span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}