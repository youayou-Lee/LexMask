// Copyright 2026 LexMask Contributors

// Issue#257 一期：step3 文件行内展开的逐页视图。
//
// 只在展开时挂载（页状态是另一个请求，不给每行都拉一遍）；轮询由
// useBatchPageStatus 负责，识别到终态即停。
// Issue#280：失败页详情面板的「人工补框」入口挂 ManualAnnotateDialog；
// manual 页的「撤销补框」走 revokeManualVerify 后刷新同一 page-status 路径。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import { ApiError } from '@/services/api-client';
import { revokeManualVerify, retryItemPages } from '@/services/jobsApi';

import { useBatchPageStatus } from '../hooks/use-batch-page-status';
import type { PipelineCfg } from '../types';
import { BatchPageMatrix } from './batch-page-matrix';
import { ManualAnnotateDialog } from './manual-annotate-dialog';

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

/** Issue#280：撤销补框错误码翻人话（同 retryErrorMessage 模式）。 */
function manualRevokeErrorMessage(t: (key: string) => string, err: unknown): string {
  if (err instanceof ApiError) {
    const key = `batchWizard.pages.manualError.${err.errorCode || 'unknown'}`;
    const text = t(key);
    if (text !== key) return text;
    return t('batchWizard.pages.manualError.unknown').replace(
      '{message}',
      err.message || err.errorCode || String(err.status),
    );
  }
  return t('batchWizard.pages.manualError.unknown').replace('{message}', String(err));
}

interface BatchRowPagesProps {
  jobId: string | null;
  itemId: string | null;
  onExportPages: (pages: number[]) => void;
  /** 后端只支持 PDF 的页级导出；image 行仍可看页格，但不给导出入口。 */
  exportable?: boolean;
  /** Issue#280：视觉类型表（人工补框对话框的类型名/色板来源）。 */
  pipelines?: PipelineCfg[];
}

export function BatchRowPages({
  jobId,
  itemId,
  onExportPages,
  exportable = true,
  pipelines = [],
}: BatchRowPagesProps) {
  const t = useT();
  const { status, error, refresh } = useBatchPageStatus({ jobId, itemId, enabled: true });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [retrying, setRetrying] = useState(false);
  const [retryError, setRetryError] = useState<string | null>(null);
  const [revokeError, setRevokeError] = useState<string | null>(null);
  // Issue#280：当前人工补框的页（null = 对话框关闭）。
  const [annotatingPage, setAnnotatingPage] = useState<number | null>(null);
  const initializedRef = useRef(false);

  useEffect(() => {
    if (!status || initializedRef.current) return;
    initializedRef.current = true;
    setSelected(
      new Set(
        status.pages
          .filter((cell) => cell.state === 'success' || cell.state === 'manual_verified')
          .map((c) => c.page),
      ),
    );
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

  // Issue#280：撤销补框 → 页回 failed → 刷新 page-status（与 retryPages 同一刷新回调）。
  const revokeManual = useCallback(
    async (page: number) => {
      if (!jobId || !itemId) return;
      setRevokeError(null);
      try {
        await revokeManualVerify(jobId, itemId, page);
        await refresh();
      } catch (err) {
        setRevokeError(manualRevokeErrorMessage(t, err));
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
        onManualAnnotate={(page) => setAnnotatingPage(page)}
        onManualRevoke={(page) => void revokeManual(page)}
        retrying={retrying}
        // Issue#314：批量审阅只在问题页展开诊断；成功页点击只勾选。
        detailPolicy="issues"
      />
      {retryError && (
        <p className="text-xs text-destructive" data-testid="page-retry-error">
          {retryError}
        </p>
      )}
      {revokeError && (
        <p className="text-xs text-destructive" data-testid="page-manual-revoke-error">
          {revokeError}
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
      {/* Issue#280：人工补框对话框（单页；file_id 取自 page-status 投影）。 */}
      {annotatingPage !== null && status.file_id && (
        <ManualAnnotateDialog
          jobId={jobId ?? ''}
          itemId={itemId ?? ''}
          fileId={status.file_id}
          page={annotatingPage}
          open
          onOpenChange={(next) => {
            if (!next) setAnnotatingPage(null);
          }}
          onVerified={() => {
            setAnnotatingPage(null);
            void refresh();
          }}
          pipelines={pipelines}
        />
      )}
    </div>
  );
}
