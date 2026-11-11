// Copyright 2026 LexMask Contributors

// Issue#257 一期：部分导出对话框（页选器 + 说明页知情确认 + 导出记录）。
//
// 与后端契约（§四）：
//  - 失败页永不进包；所选页区间内含失败页时必须显式确认 → 409 failed_pages_ack_required。
//  - 202 受理后是异步任务；状态与下载复用 /files/batch/export/{export_id}(/volumes/{name})。
//  - 记录跨重启仍在（后端扫 export-status.json），所以每次打开都重新拉一遍列表。

import { useT } from '@/i18n';
import { batchExportApi } from '@/services/api';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

import type { ItemPageStatus } from '@/services/jobsApi';
import { usePartialExportSelection } from '../hooks/use-partial-export-selection';
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
  const {
    status,
    statusError,
    refreshStatus,
    selected,
    setSelected,
    orderedSelected,
    excludedInSpan,
    records,
    recordsError,
    activeTask,
    submitting,
    actionError,
    submit,
  } = usePartialExportSelection({ jobId, itemId, enabled: open, initialSelected });

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
            {statusError && (
              <p className="text-xs text-destructive" data-testid="partial-export-status-error">
                {statusError}
              </p>
            )}

            {!status && !statusError && (
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
                  <p
                    className="rounded-lg border border-destructive/30 bg-destructive/5 px-3 py-2 text-xs text-destructive"
                    data-testid="partial-export-excluded-hint"
                  >
                    {t('batchWizard.partialExport.excludedHint')
                      .replace('{pages}', excludedInSpan.join('、'))
                      .replace('{count}', String(excludedInSpan.length))}
                  </p>
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
                      refreshStatus();
                    }}
                    disabled={submitting}
                    data-testid="partial-export-refresh"
                  >
                    {t('batchWizard.partialExport.refresh')}
                  </Button>
                  <Button
                    type="button"
                    className="h-9"
                    onClick={() => void submit()}
                    disabled={
                      submitting ||
                      !canPick || orderedSelected.length === 0
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