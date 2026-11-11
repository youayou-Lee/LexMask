// Copyright 2026 LexMask Contributors

// Issue#257 需求重对齐（2026-11-11）：单文件任务的「逐页结果与选页导出」块。
//
// 用户口径：
//  - 单文件也要能选页导出（不再只挂在批量任务上）；
//  - **识别进行中**就看得见哪一页失败、哪一页跑过——失败页/未跑页不可选；
//  - 不需要「我已了解失败页」勾选：底栏一行汇总，说明页里逐条列明；
//  - 支持 100-150 这类范围选择（页格里的范围输入 + Shift 连选）。
//
// `compact` 用于识别中的浮层（遮罩里空间有限，默认收起、展开后页格内部滚动）。

import { useMemo, useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';

import { useT } from '@/i18n';
import { batchExportApi } from '@/services/api';
import { Button } from '@/components/ui/button';
import { BatchPageMatrix } from '@/features/batch/components/batch-page-matrix';
import { usePartialExportSelection } from '@/features/batch/hooks/use-partial-export-selection';

import { useFileJobItem } from '../hooks/use-file-job-item';

interface PlaygroundPageExportProps {
  fileId: string | null;
  /** 浮层里的紧凑形态（默认收起、不展示历史记录）。 */
  compact?: boolean;
  /** 弹窗/卡片里已经自带容器：不再套外框与折叠头。 */
  bare?: boolean;
}

export function PlaygroundPageExport({
  fileId,
  compact = false,
  bare = false,
}: PlaygroundPageExportProps) {
  const t = useT();
  const { jobId, itemId } = useFileJobItem(fileId);
  const {
    status,
    statusError,
    selected,
    setSelected,
    orderedSelected,
    excludedInSpan,
    records,
    activeTask,
    submitting,
    actionError,
    submit,
  } = usePartialExportSelection({ jobId, itemId, enabled: Boolean(itemId) });
  const [open, setOpen] = useState(!compact);

  const counts = useMemo(() => {
    const tally = { success: 0, failed: 0, pending: 0 };
    status?.pages.forEach((cell) => {
      tally[cell.state] += 1;
    });
    return tally;
  }, [status]);

  // 非分页文件（docx/txt/md/图片附件…）没有页级视图，整块不出现。
  if (!status?.paged) return null;

  const total = orderedSelected.length;
  const rangeText =
    total > 1
      ? `${orderedSelected[0]}-${orderedSelected[total - 1]}`
      : `${orderedSelected[0] ?? 0}`;
  // 同一占位符在一句里可能出现两次（「已选 {count} 页 … 产物 = 说明页 1 页 + {count} 页」），
  // 只替首发会把第二个 `{count}` 原样漏到界面上；项目 tsconfig 的 lib 没有 replaceAll，
  // 用 split/join 做全量替换。
  const fill = (template: string, values: Record<string, string>): string =>
    Object.entries(values).reduce(
      (acc, [key, value]) => acc.split(`{${key}}`).join(value),
      template,
    );
  const summary = total
    ? fill(t('batchWizard.pages.exportSummary'), {
        count: String(total),
        range: rangeText,
        excludedCount: String(excludedInSpan.length),
        excludedPages: excludedInSpan.join('、'),
      })
    : t('batchWizard.pages.exportEmpty');

  const expanded = bare || open;

  return (
    <div
      className={bare ? '' : 'rounded-xl border border-border/70 bg-card px-3 py-2.5'}
      data-testid="playground-page-export"
      data-compact={compact ? 'true' : 'false'}
      data-bare={bare ? 'true' : 'false'}
    >
      {/* bare（弹窗内）不渲染折叠头：`hidden` 属性会被 Tailwind 的 `flex` 覆盖，
          条件渲染才是真的不出现。 */}
      {!bare && (
        <button
          type="button"
          className="flex w-full items-center gap-2 text-left"
          onClick={() => setOpen((prev) => !prev)}
          data-testid="playground-page-export-toggle"
          aria-expanded={expanded}
        >
          <span className="min-w-0 truncate text-sm font-medium">
            {t('batchWizard.pages.singleTitle')}
          </span>
          <span
            className="shrink-0 whitespace-nowrap text-xs text-muted-foreground tabular-nums"
            data-testid="playground-page-export-counts"
          >
            {t('batchWizard.pages.summary')
              .replace('{success}', String(counts.success))
              .replace('{failed}', String(counts.failed))
              .replace('{pending}', String(counts.pending))}
          </span>
          <span className="ml-auto shrink-0 text-muted-foreground">
            {open ? <ChevronUp className="size-4" /> : <ChevronDown className="size-4" />}
          </span>
        </button>
      )}

      {expanded && (
        <div className={`flex flex-col gap-2 ${bare ? '' : 'mt-2'}`}>
          {statusError && (
            <p className="text-xs text-destructive" data-testid="playground-page-export-error">
              {statusError}
            </p>
          )}

          <div className={compact ? 'max-h-44 overflow-y-auto' : undefined}>
            <BatchPageMatrix status={status} selected={selected} onChange={setSelected} />
          </div>

          {excludedInSpan.length > 0 && (
            <p
              className="rounded-lg border border-destructive/30 bg-destructive/5 px-2.5 py-1.5 text-xs text-destructive"
              data-testid="playground-page-export-excluded"
            >
              {t('batchWizard.partialExport.excludedHint')
                .replace('{pages}', excludedInSpan.join('、'))
                .replace('{count}', String(excludedInSpan.length))}
            </p>
          )}

          <div className="flex flex-wrap items-center gap-2">
            <span
              className="min-w-0 flex-1 text-xs text-muted-foreground"
              data-testid="playground-page-export-summary"
            >
              {summary}
            </span>
            <Button
              type="button"
              size="sm"
              className="h-8"
              onClick={() => void submit()}
              disabled={submitting || total === 0}
              data-testid="playground-page-export-submit"
            >
              {submitting
                ? t('batchWizard.partialExport.submitting')
                : t('batchWizard.partialExport.submit').replace('{count}', String(total))}
            </Button>
          </div>

          {actionError && (
            <p
              className="text-xs text-destructive"
              data-testid="playground-page-export-action-error"
            >
              {actionError}
            </p>
          )}

          {activeTask && (
            <div
              className="rounded-lg border border-border/70 bg-background px-2.5 py-1.5 text-xs"
              data-testid="playground-page-export-task"
            >
              <span className="font-medium">{activeTask.title || activeTask.export_id}</span>
              {activeTask.status === 'completed' ? (
                <span className="ml-2 flex flex-wrap items-center gap-2">
                  {activeTask.volumes.map((volume) => (
                    <a
                      key={volume.name}
                      href={batchExportApi.volumeUrl(activeTask.export_id, volume.name)}
                      className="font-medium text-primary underline underline-offset-2"
                      download
                      data-testid={`playground-page-export-download-${volume.name}`}
                    >
                      {t('batchWizard.partialExport.download')}
                    </a>
                  ))}
                </span>
              ) : (
                <span className="ml-2 text-muted-foreground">
                  {activeTask.status === 'failed'
                    ? (activeTask.error ?? t('batchWizard.partialExport.taskStatus.failed'))
                    : t(
                        activeTask.status === 'queued'
                          ? 'batchWizard.partialExport.taskStatus.queued'
                          : 'batchWizard.partialExport.taskStatus.running',
                      )}
                </span>
              )}
            </div>
          )}

          {!compact && records.length > 0 && (
            <ul className="flex flex-col gap-1" data-testid="playground-page-export-records">
              {records.slice(0, 3).map((record) => (
                <li
                  key={record.export_id}
                  className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground"
                  data-testid={`playground-page-export-record-${record.export_id}`}
                >
                  <span className="min-w-0 flex-1 truncate" title={record.title}>
                    {record.title || record.export_id}
                  </span>
                  {record.status === 'completed' &&
                    record.volumes.map((volume) => (
                      <a
                        key={volume.name}
                        href={batchExportApi.volumeUrl(record.export_id, volume.name)}
                        className="text-primary underline underline-offset-2"
                        download
                      >
                        {t('batchWizard.partialExport.download')}
                      </a>
                    ))}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
