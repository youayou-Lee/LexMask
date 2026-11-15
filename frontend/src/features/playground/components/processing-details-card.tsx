// Copyright 2026 LexMask Contributors

// Issue#194 Task 11：处理详情卡（结果页）+ 共享行渲染器。
//
// 三处一致性（spec §5）：识别进行中视图（playground-loading）、任务中心（T12）、
// 结果页详情卡都从同一个纯函数 buildProcessingRows 取行数据——本文件只做展示，
// 不重算任何映射。
//
// 旧文件条款（AC3）：performance 全空 → buildProcessingRows 返回 [] 且总耗时为
// null → 卡片返回 null（不渲染）。fileId→jobId 解析链：provider taskForFile →
// 草稿 taskJobId → 都没有则不渲染；jobId 一旦取得即 sticky（provider 账本在终态
// 短留后被清除，卡片凭已取数据留存，不闪烁）。
//
// Issue#311：默认视图不再挂载本卡片；数据解析抽成 usePlaygroundProcessingDetails，
// 结果页用它在动作行上点亮告警点、并把同一份数据交给「详情」弹窗渲染。

import { type FC, useEffect, useMemo, useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Card, CardContent } from '@/components/ui/card';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { STORAGE_KEYS } from '@/constants/storage-keys';
import { getScopedStorageItem } from '@/lib/storage';
import type { PlaygroundDraftSnapshot } from '../lib/playground-draft';
import {
  buildProcessingRows,
  buildProcessingTotalMs,
  formatDurationMs,
  isTextDirectMode,
  mergeFilePageCount,
  recognitionFailedPages,
  type ProcessingRow,
} from '../lib/processing-details';
import { useSingleTask } from '@/features/single-task/single-task-context';
import { getJob } from '@/services/jobsApi';

/** 共享行渲染器：结果页卡与进行中视图（PlaygroundLoading）共用，行文本只由行数据决定。 */
export const ProcessingRowsList: FC<{ rows: ProcessingRow[]; testId?: string }> = ({
  rows,
  testId = 'processing-rows',
}) => {
  const t = useT();
  if (rows.length === 0) return null;
  return (
    <div className="space-y-1" data-testid={testId}>
      {rows.map((row) => (
        <div
          key={row.key}
          data-testid={`processing-row-${row.key}`}
          data-tone={row.tone}
          className={cn(
            'flex items-baseline justify-between gap-3 text-xs',
            row.tone === 'warn' ? 'text-amber-600 dark:text-amber-400' : 'text-muted-foreground',
          )}
        >
          <span className="shrink-0">{t(row.labelKey)}</span>
          <span className="truncate text-right tabular-nums">
            {row.durationMs !== null && <span>{formatDurationMs(row.durationMs)}</span>}
            {row.durationMs !== null && row.counts !== null && <span className="mx-1">·</span>}
            {row.counts !== null && <span>{row.counts}</span>}
          </span>
        </div>
      ))}
    </div>
  );
};

export interface ProcessingDetailsData {
  performance: Record<string, unknown> | null;
  mergedPerf: Record<string, unknown> | null;
  rows: ProcessingRow[];
  totalMs: number | null;
  textDirect: boolean;
  failedPages: number[];
  /** 有任何可展示内容（阶段行 / 总耗时 / 失败页）。 */
  hasData: boolean;
}

/** 纯派生：performance → 行/耗时/失败页。结果页 hook 与卡片共用，保证同源。 */
function useProcessingDetailsData(
  performance: Record<string, unknown> | null | undefined,
  filePageCount: number | null,
): ProcessingDetailsData {
  const t = useT();
  const mergedPerf = useMemo(
    () => mergeFilePageCount(performance ?? null, filePageCount),
    [performance, filePageCount],
  );
  // 行内 counts 由 buildProcessingRows 内部按当前 locale 生成——t 的身份进依赖，
  // 语言切换时行随标签一起重算（否则 counts 停在旧语言）。
  const rows = useMemo(() => buildProcessingRows(mergedPerf), [mergedPerf, t]);
  const totalMs = useMemo(() => buildProcessingTotalMs(mergedPerf), [mergedPerf]);
  const textDirect = useMemo(() => isTextDirectMode(mergedPerf), [mergedPerf]);
  // Issue#199：识别失败页清单（泄漏安全——失败页零区域，必须明示「未识别」而非「无敏感内容」）。
  const failedPages = useMemo(() => recognitionFailedPages(mergedPerf), [mergedPerf]);
  const hasData = rows.length > 0 || totalMs !== null || failedPages.length > 0;
  return {
    performance: performance ?? null,
    mergedPerf,
    rows,
    totalMs,
    textDirect,
    failedPages,
    hasData,
  };
}

/** 处理详情纯展示体：不含外层 Card，供结果页详情弹窗与卡片共用。 */
export const ProcessingDetailsBody: FC<{
  data: ProcessingDetailsData;
  failed?: boolean;
  errorMessage?: string | null;
}> = ({ data, failed = false, errorMessage = null }) => {
  const t = useT();
  if (failed) {
    return (
      <div
        className="flex items-start gap-2 text-xs text-destructive"
        data-testid="processing-details-failed"
      >
        <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
        <div className="min-w-0">
          <p className="font-medium">{t('processingDetails.failed')}</p>
          {errorMessage && <p className="mt-1 break-words text-muted-foreground">{errorMessage}</p>}
        </div>
      </div>
    );
  }

  if (!data.hasData) return null;

  return (
    <div className="space-y-2" data-testid="processing-details-body">
      <div className="flex items-baseline justify-between gap-3">
        <p className="text-xs font-semibold" data-testid="processing-details-title">
          {t('processingDetails.title')}
        </p>
        {data.totalMs !== null && (
          <p
            className="text-xs font-medium tabular-nums text-foreground"
            data-testid="processing-details-total"
          >
            <span className="mr-1 font-normal text-muted-foreground">
              {t('processingDetails.total')}
            </span>
            {formatDurationMs(data.totalMs)}
          </p>
        )}
      </div>
      <ProcessingRowsList rows={data.rows} testId="processing-details-rows" />
      {data.failedPages.length > 0 && (
        <div
          className="flex items-start gap-2 text-xs text-amber-600 dark:text-amber-400"
          data-testid="processing-details-failed-pages"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
          <p className="min-w-0 break-words">
            {t('processingDetails.failedPages').replace('{pages}', data.failedPages.join(', '))}
          </p>
        </div>
      )}
      {data.textDirect && (
        <p className="text-xs text-muted-foreground" data-testid="processing-details-text-direct">
          {t('processingDetails.textDirect')}
        </p>
      )}
    </div>
  );
};

/**
 * 处理详情卡（结果页）：总耗时 + 各阶段明细。
 * - `performance` 传 getJob(jobId,{performance:true}) 的 item.performance；
 * - `filePageCount` 供文本件补 page_count（GET /files/{id}）；
 * - failed 变体：失败态 + 人话错误信息（结果页不触发，任务中心/识别页用）。
 */
export const ProcessingDetailsCard: FC<{
  performance?: Record<string, unknown> | null;
  filePageCount?: number | null;
  failed?: boolean;
  errorMessage?: string | null;
}> = ({ performance, filePageCount = null, failed = false, errorMessage = null }) => {
  const data = useProcessingDetailsData(performance, filePageCount);

  // AC3 旧文件条款：无任何阶段数据且无失败页且非失败态 → 不渲染卡。
  if (!failed && !data.hasData) return null;

  return (
    <Card className="mb-3 flex-shrink-0 px-4 py-3" data-testid="processing-details-card">
      <CardContent className="space-y-2 p-0">
        <ProcessingDetailsBody data={data} failed={failed} errorMessage={errorMessage} />
      </CardContent>
    </Card>
  );
};

/** 草稿里的 taskJobId（Issue#194）：仅当草稿属于该文件时才认。 */
function draftTaskJobIdFor(fileId: string): string | null {
  const snapshot = getScopedStorageItem<PlaygroundDraftSnapshot | null>(
    STORAGE_KEYS.PLAYGROUND_DRAFT,
    null,
  );
  if (!snapshot || snapshot.fileInfo?.file_id !== fileId) return null;
  return typeof snapshot.taskJobId === 'string' && snapshot.taskJobId ? snapshot.taskJobId : null;
}

export interface PlaygroundProcessingDetailsState extends ProcessingDetailsData {
  jobId: string | null;
  /** 结果页拿到的原卷页数（与卡内派生同源；Issue#311 门⑥ I2）。 */
  filePageCount: number | null;
  failed: boolean;
  errorMessage: string | null;
}

/**
 * 结果页处理详情取数：fileId → jobId（provider taskForFile ?? sticky ?? 草稿）
 * → getJob(performance) → item.performance，再派生行/耗时/失败页。
 * jobId sticky：provider 账本终态短留清除后卡片不消失、不重复请求。
 */
export function usePlaygroundProcessingDetails(
  fileId: string | null,
  filePageCount: number | null = null,
): PlaygroundProcessingDetailsState {
  const { taskForFile } = useSingleTask();
  const task = fileId ? taskForFile(fileId) : undefined;
  const providerJobId = task?.jobId ?? null;

  // jobId 一旦取得就按文件记住（provider 清除条目后仍可用）；按 fileId 键控，跨文件不串。
  const [stickyByFile, setStickyByFile] = useState<Record<string, string>>({});
  useEffect(() => {
    if (fileId && providerJobId) {
      setStickyByFile((prev) =>
        prev[fileId] === providerJobId ? prev : { ...prev, [fileId]: providerJobId },
      );
    }
  }, [fileId, providerJobId]);

  const jobId =
    providerJobId ??
    (fileId ? stickyByFile[fileId] ?? null : null) ??
    (fileId ? draftTaskJobIdFor(fileId) : null);

  const [itemPerformance, setItemPerformance] = useState<Record<string, unknown> | null>(null);
  useEffect(() => {
    setItemPerformance(null);
    if (!jobId) return;
    let cancelled = false;
    getJob(jobId, { performance: true })
      .then((detail) => {
        if (cancelled) return;
        const item =
          detail.items.find((candidate) => candidate.file_id === fileId) ?? detail.items[0];
        setItemPerformance(item?.performance ?? null);
      })
      .catch(() => {
        if (!cancelled) setItemPerformance(null);
      });
    return () => {
      cancelled = true;
    };
  }, [jobId, fileId]);

  const data = useProcessingDetailsData(itemPerformance, filePageCount);

  return {
    ...data,
    jobId,
    filePageCount,
    failed: task?.phase === 'failed',
    errorMessage: task?.error ?? null,
  };
}

/**
 * 结果页挂载壳（兼容既有调用方 / 测试）：解析链与 usePlaygroundProcessingDetails
 * 完全一致，结果页默认视图直接使用 hook（不再挂载卡片）。
 */
export const PlaygroundProcessingDetails: FC<{
  fileId: string | null;
  filePageCount?: number | null;
}> = ({ fileId, filePageCount = null }) => {
  const processing = usePlaygroundProcessingDetails(fileId, filePageCount);
  if (!fileId || !processing.jobId) return null;
  return (
    <ProcessingDetailsCard
      performance={processing.performance}
      filePageCount={filePageCount}
      failed={processing.failed}
      errorMessage={processing.errorMessage}
    />
  );
};
