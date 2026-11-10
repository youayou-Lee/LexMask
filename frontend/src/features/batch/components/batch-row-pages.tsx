// Copyright 2026 LexMask Contributors

// Issue#257 一期：step3 文件行内展开的逐页视图。
//
// 只在展开时挂载（页状态是另一个请求，不给每行都拉一遍）；轮询由
// useBatchPageStatus 负责，识别到终态即停。

import { useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';

import { useBatchPageStatus } from '../hooks/use-batch-page-status';
import { BatchPageMatrix } from './batch-page-matrix';

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
  const { status, error } = useBatchPageStatus({ jobId, itemId, enabled: true });
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const initializedRef = useRef(false);

  useEffect(() => {
    if (!status || initializedRef.current) return;
    initializedRef.current = true;
    setSelected(new Set(status.pages.filter((cell) => cell.state === 'success').map((c) => c.page)));
  }, [status]);

  const orderedSelected = useMemo(() => [...selected].sort((a, b) => a - b), [selected]);

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
      <BatchPageMatrix status={status} selected={selected} onChange={setSelected} />
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