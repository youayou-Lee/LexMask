// Copyright 2026 LexMask Contributors

// Issue#257 一期：页格三态矩阵（✅ 成功 / ❌ 失败 / ⚪ 未跑）。
//
// 语义（与后端 page-status 投影一致）：
//  - success = 该页 recognition.pages[p].status == completed
//  - failed  = status failed 或 timeout（timeout 对用户就是失败，原因码区分在提示里）
//  - pending = 还没有该页的终态记录（未跑到或正在跑）
// 失败页**不可选**：#199 红线规定失败页不进交付，这里从选择层面就挡住。

import { useEffect, useMemo, useState } from 'react';

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
  /** 单页重试（能力 2）：点击红叉页触发，仅识别完成且未被占用时可用。 */
  onRetryPage?: (page: number) => void;
  /** 整批重试失败页；与 onRetryPage 同时给出时渲染工具栏按钮。 */
  onRetryFailed?: (pages: number[]) => void;
  retrying?: boolean;
}

/** 首次渲染的页格数：200+ 页整卷一次性铺 DOM 会明显拖慢展开（评审 Important-3）。 */
const PAGE_CHUNK = 50;

const STATE_STYLES: Record<PageCellState, string> = {
  success: 'border-[var(--success-border)] bg-[var(--success-surface)] text-[var(--success-foreground)]',
  failed: 'border-destructive/40 bg-destructive/10 text-destructive',
  pending: 'border-border bg-muted text-muted-foreground',
};

export function BatchPageMatrix({
  status,
  selected,
  onChange,
  disabled = false,
  onRetryPage,
  onRetryFailed,
  retrying = false,
}: BatchPageMatrixProps) {
  const t = useT();
  const [visibleCount, setVisibleCount] = useState(PAGE_CHUNK);
  useEffect(() => {
    setVisibleCount(PAGE_CHUNK);
  }, [status.item_id, status.page_count]);
  const visiblePages = useMemo(
    () => status.pages.slice(0, visibleCount),
    [status.pages, visibleCount],
  );
  const hiddenCount = status.pages.length - visiblePages.length;
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
  // 逐页判断而不是比 size：选择集可能残留已转 failed 的页（size 相等会误判「已全选」）。
  const allSelected =
    allSuccess.size > 0 && [...allSuccess].every((page) => selected.has(page));

  // 只有「识别已完成、且没有被重试任务占着」时才允许重试；
  // recognition_state 为 running 时后端也会 409 item_busy，这里提前挡住。
  const retryable = status.recognition_state === 'done' && !retrying;
  const failedPages = useMemo(
    () => status.pages.filter((cell) => cell.state === 'failed').map((cell) => cell.page),
    [status.pages],
  );

  const activate = (page: number, state: PageCellState) => {
    if (state === 'failed') {
      if (retryable && onRetryPage) onRetryPage(page);
      return;
    }
    toggle(page, state);
  };

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
          {onRetryFailed && failedPages.length > 0 && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-7 px-2 text-xs"
              disabled={!retryable}
              onClick={() => onRetryFailed(failedPages)}
              data-testid="page-matrix-retry-failed"
            >
              {retrying
                ? t('batchWizard.pages.retrying')
                : t('batchWizard.pages.retryFailed').replace('{count}', String(failedPages.length))}
            </Button>
          )}
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
          {visiblePages.map((cell) => {
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
                  cell.state === 'failed' && retryable
                    ? 'cursor-pointer'
                    : cell.state === 'failed'
                      ? 'cursor-not-allowed'
                      : 'cursor-pointer',
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
                aria-disabled={
                  cell.state === 'success' ? disabled : !(cell.state === 'failed' && retryable)
                }
                data-testid={`page-cell-${cell.page}`}
                data-state={cell.state}
                data-selected={isSelected ? 'true' : 'false'}
                data-retryable={cell.state === 'failed' && retryable ? 'true' : 'false'}
                onClick={() => activate(cell.page, cell.state)}
              >
                {cell.page}
              </button>
            );
          })}
        </div>
        {hiddenCount > 0 && (
          <div className="flex justify-center pt-1.5">
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-7 px-2 text-xs"
              onClick={() => setVisibleCount((n) => n + PAGE_CHUNK)}
              data-testid="page-matrix-show-more"
            >
              {t('batchWizard.pages.showMore').replace('{count}', String(hiddenCount))}
            </Button>
          </div>
        )}
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
          {' · '}
          {retryable
            ? t('batchWizard.pages.retryHint')
            : t('batchWizard.pages.retryUnavailable')}
        </p>
      )}
    </div>
  );
}