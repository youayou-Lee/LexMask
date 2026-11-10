// Copyright 2026 LexMask Contributors

// Issue#257 一期：step3 文件行内展开的逐页视图。
//
// 只在展开时挂载（页状态是另一个请求，不给每行都拉一遍）；轮询由
// useBatchPageStatus 负责，识别到终态即停。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/services/api-client';
import { retryItemPages } from '@/services/jobsApi';

import { useBatchPageStatus } from '../hooks/use-batch-page-status';
import { BatchPageMatrix } from './batch-page-matrix';

/** 把后端重试错误码翻成人话；未知码带上原文，别把用户卡在死胡同里。 */
function retryErrorMessage(t: (key: string) => string, err: unknown): string {
  if (err instanceof ApiError) {
    const key = `batchWizard.pages.retryError.${err.errorCode || 'unknown'}`;
    const text = t(key);
    if (text !== key) return text;
    return t('batchWizard.pages.retryFailedError').replace(
      '{message}',
      err.message || err.errorCode || String(err.status),
    );
  }
  return t('batchWizard.pages.retryFailedError').replace('{message}', String(err));
}

interface BatchRowPagesProps {
  jobId: string | null;
  itemId: string | null;
  onExportPages: (pages: number[]) => void;
  /** 后端只支持 PDF 的页级导出；image 行仍可看页格，但不给导出入口。 */
  exportable?: boolean;
}

export function BatchRowPages({
  jobId,
  itemId,
  onExportPages,
  exportable = true,
}: BatchRowPagesProps) {
  const t = useT();
  const { status, error, refresh } = useBatchPageStatus({ jobId, itemId, enabled: true });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const initializedRef = useRef(false);

  useEffect(() => {
    if (!status || initializedRef.current) return;
    initializedRef.current = true;
    setSelected(new Set(status.pages.filter((cell) => cell.state === 'success').map((c) => c.page)));
  }, [status]);

  const orderedSelected = useMemo(() => [...selected].sort((a, b) => a - b), [selected]);

  const retryPages = useCallback(
    async (pages: number[]) => {
      if (!jobId || !itemId || pages.length === 0) return;
      setRetrying(true);
      setRetryError(null);
      try {
        await retryItemPages(jobId, itemId, pages);
        await refresh();
      } catch (err) {
        setRetryError(retryErrorMessage(t, err));
      } finally {
        setRetrying(false);
      }
    },
    [itemId, jobId, refresh, t],
  );

  if (error) {
    return (
      <p className="text-xs text-destructive" data-testid="page-matrix-error">
        {error}
      </p>
    );
  }
  if (!status) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="page-matrix-loading">
        {t('batchWizard.pages.loading')}
      </p>
    );
  }
  if (!status.paged) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="page-matrix-unsupported">
        {t('batchWizard.pages.unsupported')}
      </p>
    );
  }

  return (
    <div className="flex flex-col gap-2" data-testid="page-matrix-block">
      <BatchPageMatrix
        status={status}
        selected={selected}
        onChange={setSelected}
        onRetryPage={(page) => void retryPages([page])}
        onRetryFailed={(pages) => void retryPages(pages)}
        retrying={retrying}
      />
      {retryError && (
        <p className="text-xs text-destructive" data-testid="page-retry-error">
          {retryError}
        </p>
      )}
      <div className="flex items-center justify-end gap-2">
        {exportable ? (
          <Button
            type="button"
            variant="outline"
            className="h-8 text-xs"
            disabled={orderedSelected.length === 0}
            onClick={() => onExportPages(orderedSelected)}
            data-testid="page-matrix-export"
          >
            {t('batchWizard.pages.partialExport')}（{orderedSelected.length}）
          </Button>
        ) : (
          <span className="text-xs text-muted-foreground" data-testid="page-matrix-export-unsupported">
            {t('batchWizard.pages.exportPdfOnly')}
          </span>
        )}
      </div>
    </div>
  );
}