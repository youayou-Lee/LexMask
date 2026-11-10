// Copyright 2026 LexMask Contributors

// Issue#257 一期：页格三态矩阵（✅ 成功 / ❌ 失败 / ⚪ 未跑）。
//
// 语义（与后端 page-status 投影一致）：
//  - success = 该页 recognition.pages[p].status == completed
//  - failed  = status failed 或 timeout（timeout 对用户就是失败，原因码区分在提示里）
//  - pending = 还没有该页的终态记录（未跑到或正在跑）
// 失败页**不可选**：#199 红线规定失败页不进交付，这里从选择层面就挡住。

import { useMemo } from 'react';

import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';

import type { ItemPageStatus, PageCellState } from '@/services/jobsApi';

interface BatchPageMatrixProps {
  status: ItemPageStatus;
  selected: ReadonlySet<number>;
  onChange: (next: Set<number>) => void;
  /** 只读（例如导出任务已提交、或识别进行中禁用勾选）。 */
  disabled?: boolean;
}

const STATE_STYLES: Record<PageCellState, string> = {
  success: 'border-[var(--success-border)] bg-[var(--success-surface)] text-[var(--success-foreground)]',
  failed: 'border-destructive/40 bg-destructive/10 text-destructive',
  pending: 'border-border bg-muted text-muted-foreground',
};

export function BatchPageMatrix({ status, selected, onChange, disabled = false }: BatchPageMatrixProps) {
  const t = useT();
  const counts = useMemo(() => {
    const tally = { success: 0, failed: 0, pending: 0 };
    status.pages.forEach((cell) => {
      tally[cell.state] += 1;
    });
    return tally;
  }, [status.pages]);

  const allSuccess = useMemo(
    () => new Set(status.pages.filter((cell) => cell.state === 'success').map((cell) => cell.page)),
    [status.pages],
  );
  const allSelected = allSuccess.size > 0 && selected.size === allSuccess.size;

  const toggle = (page: number, state: PageCellState) => {
    if (disabled || state !== 'success') return;
    const next = new Set(selected);
    if (next.has(page)) next.delete(page);
    else next.add(page);
    onChange(next);
  };

  const stateLabel = (state: PageCellState) => {
    if (state === 'success') return t('batchWizard.pages.stateSuccess');
    if (state === 'failed') return t('batchWizard.pages.stateFailed');
    return t('batchWizard.pages.statePending');
  };

  return (
    <div className="flex flex-col gap-2" data-testid="page-matrix">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs">
        <span className="tabular-nums" data-testid="page-matrix-summary">
          {t('batchWizard.pages.summary')
            .replace('{success}', String(counts.success))
            .replace('{failed}', String(counts.failed))
            .replace('{pending}', String(counts.pending))}
        </span>
        <span className="text-muted-foreground tabular-nums" data-testid="page-matrix-selected">
          {t('batchWizard.pages.selectedCount').replace('{count}', String(selected.size))}
        </span>
        <div className="ml-auto flex gap-1.5">
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-7 px-2 text-xs"
            disabled={disabled || allSelected || allSuccess.size === 0}
            onClick={() => onChange(new Set(allSuccess))}
            data-testid="page-matrix-select-all"
          >
            {t('batchWizard.pages.selectAll')}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-7 px-2 text-xs"
            disabled={disabled || selected.size === 0}
            onClick={() => onChange(new Set())}
            data-testid="page-matrix-select-none"
          >
            {t('batchWizard.pages.selectNone')}
          </Button>
        </div>
      </div>

      <div className="max-h-56 overflow-y-auto rounded-lg border border-border/70 bg-background p-1.5">
        <div className="grid grid-cols-[repeat(auto-fill,minmax(2rem,1fr))] gap-1">
          {status.pages.map((cell) => {
            const isSelected = selected.has(cell.page);
            const reasonLabel = cell.reason ? `：${cell.reason}` : '';
            return (
              <button
                key={cell.page}
                type="button"
                className={cn(
                  'flex h-8 items-center justify-center rounded-md border text-[11px] font-medium tabular-nums transition-colors',
                  STATE_STYLES[cell.state],
                  isSelected && 'ring-2 ring-primary ring-offset-1 ring-offset-background',
                  cell.state === 'failed' ? 'cursor-not-allowed' : 'cursor-pointer',
                  disabled && 'cursor-not-allowed opacity-60',
                )}
                title={t('batchWizard.pages.cellLabel')
                  .replace('{page}', String(cell.page))
                  .replace('{state}', stateLabel(cell.state))
                  .replace('{reason}', reasonLabel)}
                aria-label={t('batchWizard.pages.cellLabel')
                  .replace('{page}', String(cell.page))
                  .replace('{state}', stateLabel(cell.state))
                  .replace('{reason}', reasonLabel)}
                aria-pressed={isSelected}
                aria-disabled={disabled || cell.state !== 'success'}
                data-testid={`page-cell-${cell.page}`}
                data-state={cell.state}
                data-selected={isSelected ? 'true' : 'false'}
                onClick={() => toggle(cell.page, cell.state)}
              >
                {cell.page}
              </button>
            );
          })}
        </div>
      </div>

      {counts.failed > 0 && (
        <p className="text-xs text-destructive" data-testid="page-matrix-failed-hint">
          {t('batchWizard.pages.failedHint').replace(
            '{pages}',
            status.pages
              .filter((cell) => cell.state === 'failed')
              .map((cell) => String(cell.page))
              .join('、'),
          )}
        </p>
      )}
    </div>
  );
}