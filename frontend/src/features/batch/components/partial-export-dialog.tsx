// Copyright 2026 LexMask Contributors

// Issue#278 统一导出对话框（原 Issue#257 部分导出对话框就地改造）：
//  - 模式段：完整导出（主产物直取、失败页自动排除）｜选页导出（页格 + 区间排除提示）；
//  - 说明.txt 开关（默认关）：说明与正文分离，正文 PDF 不含任何插入页；
//  - 「回到编辑」常驻按钮：跳回审阅画布，改完回来选择集保留（hook cache）；
//  - 进度卡：只报服务端真实 stage/current/total（#238 诚实化纪律），不做本地估算；
//  - 记录列表：kind 徽标 + 双口径摘要（legacy partial_redaction 保留 +1 补偿）。

import { useEffect } from 'react';

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

import type { ItemExportTask, ItemPageStatus } from '@/services/jobsApi';
import { useItemExport, type ItemExportKind } from '../hooks/use-item-export';
import { BatchPageMatrix } from './batch-page-matrix';

interface PartialExportDialogProps {
  open: boolean;
  jobId: string | null;
  itemId: string | null;
  filename: string;
  /** 缺省模式；从 step5 单文件进入时缺省 = 完整导出。 */
  defaultKind?: ItemExportKind;
  /** 行内页选器带上来的页号；为空则默认全选可导出页。 */
  initialSelected?: number[] | null;
  /** 「回到编辑」：跳回审阅画布（宿主负责 flush 草稿与导航）。 */
  onBackToEdit?: () => void;
  onClose: () => void;
}

// 动态拼 key 会在缺键时把 key 原样吐到界面（i18n 的兜底就是返回 key），
// 所以状态文案走显式映射；未知状态直出原始字符串。
function taskStatusKey(status: string): string {
  switch (status) {
    case 'queued':
      return 'batchWizard.export.taskStatus.queued';
    case 'running':
      return 'batchWizard.export.taskStatus.running';
    case 'completed':
      return 'batchWizard.export.taskStatus.completed';
    case 'failed':
      return 'batchWizard.export.taskStatus.failed';
    default:
      return '';
  }
}

/** kind 徽标：legacy partial_redaction 单独标注（旧口径记录）。 */
function kindBadgeKey(kind: string): string {
  if (kind === 'full') return 'batchWizard.export.kindFull';
  if (kind === 'partial') return 'batchWizard.export.kindPartial';
  return 'batchWizard.export.kindLegacy';
}

function progressStageText(t: (key: string) => string, task: ItemExportTask): string {
  const stage = task.progress?.stage ?? '';
  const current = Number(task.progress?.current ?? 0);
  const total = Number(task.progress?.total ?? 0);
  if (stage === 'rendering') {
    return t('batchWizard.export.progressRendering')
      .replace('{current}', String(current))
      .replace('{total}', String(total));
  }
  if (stage === 'packaging') return t('batchWizard.export.progressPackaging');
  if (stage === 'completed') return t('batchWizard.export.progressDone');
  return taskStatusKey(stage) ? t(taskStatusKey(stage)) : stage;
}

export function PartialExportDialog({
  open,
  jobId,
  itemId,
  filename,
  defaultKind = 'partial',
  initialSelected = null,
  onBackToEdit,
  onClose,
}: PartialExportDialogProps) {
  const t = useT();
  const {
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
    excludedInSpan,
    records,
    recordsError,
    activeTask,
    submitting,
    actionError,
    submit,
  } = useItemExport({ jobId, itemId, enabled: open, kind: defaultKind, initialSelected });

  // 宿主切换（step3 行内入口 vs step5 单文件入口）时同步缺省模式。
  useEffect(() => {
    if (open) setKind(defaultKind);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只在 open/defaultKind 变化时重置
  }, [open, defaultKind]);

  const paged = status?.paged ?? false;
  const recognitionState = status?.recognition_state ?? 'not_started';
  // 识别进行中也允许导出（跑到 100 页就能先导前 100 页）；
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
            <DialogTitle>{t('batchWizard.export.title')}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 truncate text-sm leading-relaxed">
              <span className="min-w-0 flex-1 truncate font-medium text-foreground">{filename}</span>
              {onBackToEdit && (
                <button
                  type="button"
                  className="shrink-0 text-xs font-medium text-primary underline underline-offset-2"
                  onClick={onBackToEdit}
                  data-testid="export-back-to-edit"
                >
                  {t('batchWizard.export.backToEdit')}
                </button>
              )}
            </DialogDescription>
          </DialogHeader>

          <div className="flex min-h-0 flex-col gap-3 overflow-y-auto px-5 py-4">
            {statusError && (
              <p className="text-xs text-destructive" data-testid="partial-export-status-error">
                {statusError}
              </p>
            )}

            {!status && !statusError && (
              <p className="text-xs text-muted-foreground">{t('batchWizard.export.loading')}</p>
            )}

            {status && !paged && (
              <p className="text-xs text-muted-foreground" data-testid="partial-export-unsupported">
                {t('batchWizard.export.unsupportedType')}
              </p>
            )}

            {status && (
              <>
                {/* 模式段（data-testid="export-mode"） */}
                <div
                  className="flex gap-1.5"
                  role="tablist"
                  aria-label={t('batchWizard.export.title')}
                  data-testid="export-mode"
                >
                  <Button
                    type="button"
                    variant={kind === 'full' ? 'default' : 'outline'}
                    size="sm"
                    className="h-8"
                    onClick={() => setKind('full')}
                    data-testid="export-mode-full"
                  >
                    {t('batchWizard.export.modeFull')}
                  </Button>
                  <Button
                    type="button"
                    variant={kind === 'partial' ? 'default' : 'outline'}
                    size="sm"
                    className="h-8"
                    onClick={() => setKind('partial')}
                    data-testid="export-mode-partial"
                  >
                    {t('batchWizard.export.modePartial')}
                  </Button>
                </div>
                {kind === 'full' && (
                  <p className="text-xs text-muted-foreground" data-testid="export-full-desc">
                    {t('batchWizard.export.fullDesc')}
                  </p>
                )}

                {kind === 'partial' && paged && (
                  <>
                    {recognitionState === 'running' && (
                      <p className="text-xs text-muted-foreground" data-testid="partial-export-running">
                        {t('batchWizard.export.runningHint')}
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
                        {t('batchWizard.export.excludedHint')
                          .replace('{pages}', excludedInSpan.join('、'))
                          .replace('{count}', String(excludedInSpan.length))}
                      </p>
                    )}
                  </>
                )}

                {kind === 'partial' && !paged && (
                  <p className="text-xs text-muted-foreground">
                    {t('batchWizard.export.unsupportedType')}
                  </p>
                )}

                {/* 说明.txt 开关（默认关） */}
                <label
                  className="flex items-start gap-2 text-xs text-muted-foreground"
                  data-testid="export-include-summary-row"
                >
                  <input
                    type="checkbox"
                    className="mt-0.5 size-3.5 accent-[var(--primary)]"
                    checked={includeSummary}
                    onChange={(event) => setIncludeSummary(event.target.checked)}
                    data-testid="export-include-summary"
                  />
                  <span className="min-w-0">
                    <span className="font-medium text-foreground">
                      {t('batchWizard.export.includeSummary')}
                    </span>
                    <span className="block">{t('batchWizard.export.summaryFileNote')}</span>
                  </span>
                </label>

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
                      {activeTask.status === 'queued' || activeTask.status === 'running' ? (
                        <span className="tabular-nums" data-testid="export-progress">
                          {progressStageText(t, activeTask)}
                        </span>
                      ) : null}
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
                            {t('batchWizard.export.download')}
                          </a>
                        ))}
                        {(activeTask.files ?? []).map((file) => (
                          <span
                            key={file.name}
                            className="text-muted-foreground tabular-nums"
                            data-testid={`export-sha256-${file.name}`}
                            title={`${file.name} sha256=${file.sha256}`}
                          >
                            {t('batchWizard.export.sha256').replace('{tail}', file.sha256.slice(-8))}
                          </span>
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
                    {t('batchWizard.export.refresh')}
                  </Button>
                  <Button
                    type="button"
                    className="h-9"
                    onClick={() => void submit()}
                    disabled={
                      submitting ||
                      (kind === 'partial' && (!canPick || orderedSelected.length === 0))
                    }
                    data-testid="partial-export-submit"
                  >
                    {submitting
                      ? t('batchWizard.export.submitting')
                      : kind === 'full'
                        ? t('batchWizard.export.submitFull')
                        : t('batchWizard.export.submit').replace(
                            '{count}',
                            String(orderedSelected.length),
                          )}
                  </Button>
                </div>
              </>
            )}

            <section className="border-t border-border/70 pt-3" data-testid="partial-export-records">
              <h3 className="text-xs font-medium">{t('batchWizard.export.recordsTitle')}</h3>
              {recordsError && <p className="mt-1 text-xs text-destructive">{recordsError}</p>}
              {records.length === 0 ? (
                <p className="mt-1 text-xs text-muted-foreground">
                  {t('batchWizard.export.recordsEmpty')}
                </p>
              ) : (
                <ul className="mt-1.5 flex flex-col gap-1.5">
                  {records.map((record) => {
                    const isLegacy = record.kind === 'partial_redaction';
                    const inPackagePages = isLegacy
                      ? (record.pages?.length ?? 0) + 1 // legacy 产物含说明页 1 页：保留旧口径补偿
                      : (record.page_count ?? 0);
                    return (
                      <li
                        key={record.export_id}
                        className="flex flex-wrap items-center gap-2 rounded-md border border-border/60 px-2.5 py-1.5 text-xs"
                        data-testid={`partial-export-record-${record.export_id}`}
                      >
                        <span
                          className="shrink-0 rounded border border-border/60 px-1 py-px text-[10px] text-muted-foreground"
                          data-testid={`partial-export-record-kind-${record.export_id}`}
                        >
                          {t(kindBadgeKey(record.kind))}
                        </span>
                        <span className="min-w-0 flex-1 truncate font-medium" title={record.title}>
                          {record.title || record.export_id}
                        </span>
                        {inPackagePages > 0 || (record.pages?.length ?? 0) > 0 ? (
                          <span
                            className="shrink-0 text-muted-foreground tabular-nums"
                            data-testid={`partial-export-record-meta-${record.export_id}`}
                          >
                            {t('batchWizard.export.recordSummary')
                              .replace('{pages}', String(inPackagePages))
                              .replace('{total}', String(record.page_count ?? 0))
                              .replace(
                                '{excluded}',
                                String(record.excluded_failed_pages?.length ?? 0),
                              )}
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
                              {t('batchWizard.export.download')}
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
                    );
                  })}
                </ul>
              )}
            </section>
          </div>
        </DialogContent>
      ) : null}
    </Dialog>
  );
}
