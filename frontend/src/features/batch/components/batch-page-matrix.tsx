// Copyright 2026 LexMask Contributors

// Issue#257 一期：页格三态矩阵（✅ 成功 / ❌ 失败 / ⚪ 未跑）。
//
// 语义（与后端 page-status 投影一致）：
//  - success = 该页 recognition.pages[p].status == completed
//  - failed  = status failed 或 timeout（timeout 对用户就是失败，原因码区分在提示里）
//  - pending = 还没有该页的终态记录（未跑到或正在跑）
// 失败页**不可选**：#199 红线规定失败页不进交付，这里从选择层面就挡住。
//
// Issue#279：点页格展开「这页发生了什么」详情面板（路由 / 各阶段耗时 / 机读失败
// 类型 / 尝试次数），并在卷头渲染路由摘要 chip。数据全部来自既有 page-status 响应
// 扩展字段（A4.3：不新增 JSONL 读取接口）。
// Issue#289：「看」与「重试」拆分——点击任何格子（含失败页）只展开/收起详情；
// 重试收敛到失败页详情面板内的显式按钮（page-detail-retry），格子不再触发重试。

import { useEffect, useMemo, useRef, useState } from 'react';

import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';

import type { ItemPageStatus, PageCellState } from '@/services/jobsApi';

import { extendSelection, parsePageRangeInput, pruneSelection } from '../lib/page-range';

interface BatchPageMatrixProps {
  status: ItemPageStatus;
  selected: ReadonlySet<number>;
  onChange: (next: Set<number>) => void;
  /** 只读（例如导出任务已提交、或识别进行中禁用勾选）。 */
  disabled?: boolean;
  /** 单页重试（能力 2）：由失败页详情面板内的显式按钮触发（Issue#289 拆分），仅识别完成且未被占用时可用。 */
  onRetryPage?: (page: number) => void;
  /** 整批重试失败页；与 onRetryPage 同时给出时渲染工具栏按钮。 */
  onRetryFailed?: (pages: number[]) => void;
  retrying?: boolean;
  /**
   * 识别进行中（仅单文件入口会用）：状态每 3 秒刷新，某页可能从成功变失败，
   * 选择集要跟着失效——组件内部按 ``pages`` 变化剪枝。
   */
  pruneOnChange?: boolean;
}

/** 首次渲染的页格数：200+ 页整卷一次性铺 DOM 会明显拖慢展开（评审 Important-3）。 */
const PAGE_CHUNK = 50;

const STATE_STYLES: Record<PageCellState, string> = {
  success: 'border-[var(--success-border)] bg-[var(--success-surface)] text-[var(--success-foreground)]',
  failed: 'border-destructive/40 bg-destructive/10 text-destructive',
  pending: 'border-border bg-muted text-muted-foreground',
};

/** 已知 docline fallback 原因码的 i18n 键空间；未知码直显原文（同 retryError 的处理）。 */
const FALLBACK_REASON_KEYS: Record<string, string> = {
  watermark_overlay_hit: 'pageDetail.fallbackReason.watermark_overlay_hit',
  no_text_layer: 'pageDetail.fallbackReason.no_text_layer',
  build_budget: 'pageDetail.fallbackReason.build_budget',
  verdict_unusable: 'pageDetail.fallbackReason.verdict_unusable',
  repeated_fragment_coverage: 'pageDetail.fallbackReason.repeated_fragment_coverage',
};

/** 已知 stage_ms 键的人话；未知键（含 `pipeline.key` 前缀形态）直显原文。 */
const STAGE_LABEL_KEYS: Record<string, string> = {
  ocr_has: 'pageDetail.stage.ocr_has',
  ner: 'pageDetail.stage.ner',
  mineru: 'pageDetail.stage.mineru',
  vision: 'pageDetail.stage.vision',
};

export function BatchPageMatrix({
  status,
  selected,
  onChange,
  disabled = false,
  onRetryPage,
  onRetryFailed,
  retrying = false,
  pruneOnChange = true,
}: BatchPageMatrixProps) {
  const t = useT();
  const [visibleCount, setVisibleCount] = useState(PAGE_CHUNK);
  const [rangeText, setRangeText] = useState('');
  // Issue#279：当前展开详情的页（单槽——点另一页即换页展开，再点同页收起）。
  const [expandedPage, setExpandedPage] = useState<number | null>(null);
  // Shift 连选的锚点：点过的上一页（只在可选页上更新）。
  const anchorRef = useRef<number | null>(null);
  useEffect(() => {
    setVisibleCount(PAGE_CHUNK);
    setExpandedPage(null);
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
  const selectablePages = useMemo(
    () => new Set(status.pages.filter((cell) => cell.state === 'success').map((cell) => cell.page)),
    [status.pages],
  );
  const isSelectable = (page: number) => !disabled && selectablePages.has(page);

  // 运行中刷新：某页从成功变失败后立刻从选择集里摘掉（用户看到的「已选 N 页」就是真能导出的）。
  useEffect(() => {
    if (!pruneOnChange) return;
    const pruned = pruneSelection(selected, (page) => selectablePages.has(page));
    if (pruned !== selected) onChange(pruned);
  }, [onChange, pruneOnChange, selectablePages, selected]);

  // 逐页判断而不是比 size：选择集可能残留已转 failed 的页（size 相等会误判「已全选」）。
  const allSelected =
    allSuccess.size > 0 && [...allSuccess].every((page) => selected.has(page));

  // 只有「识别已完成、且没有被重试任务占着」时才允许重试；
  // recognition_state 为 running 时后端也会 409 item_busy，这里提前挡住。
  // 页级重试只认「识别完成、待审阅」：「已完成/已确认」的条目状态机没有回边，
  // 后端会 409 retry_requires_awaiting_review —— 前端不能给一个点了必错的按钮。
  const retryable = status.item_status === 'awaiting_review' && !retrying;
  // Issue#289：「忙」= 识别在跑或条目被重试占用（后端此态 409 item_busy）；其余
  // !retryable 即终态（completed/confirmed 等，状态机没有回边）。两者都禁用重试，
  // 区分只为提示文案准确——面板按钮旁的提示与底部 hint 同口径（方案 1.2(b)/(d)）。
  const busy = status.recognition_state === 'running' || status.item_status === 'processing';
  const failedPages = useMemo(
    () => status.pages.filter((cell) => cell.state === 'failed').map((cell) => cell.page),
    [status.pages],
  );

  // Issue#279：fallback 原因码/stage 键翻人话；字典没有的码直显原文（translate 缺键返回键本身）。
  const humanizeFallback = (code: string) => {
    const key = FALLBACK_REASON_KEYS[code];
    if (!key) return code;
    const text = t(key);
    return text === key ? code : text;
  };
  const humanizeStage = (stage: string) => {
    const key = STAGE_LABEL_KEYS[stage];
    if (!key) return stage;
    const text = t(key);
    return text === key ? stage : text;
  };

  const activate = (page: number, state: PageCellState, shiftKey = false) => {
    // 详情展开与选择是并行的两件事：Shift 连选是批量操作，不顺手换展开页。
    // Issue#289：点击（含失败页）只负责展开/收起详情——「看」与「重试」拆分，
    // 重试由详情面板内的显式按钮（page-detail-retry）触发，格子不再调 onRetryPage。
    if (!shiftKey) setExpandedPage((prev) => (prev === page ? null : page));
    if (state === 'failed') return; // #199 红线：失败页永不进选择集（与 toggle 的守卫互为冗余）
    toggle(page, state, shiftKey);
  };

  const toggle = (page: number, state: PageCellState, shiftKey = false) => {
    if (disabled || state !== 'success') return;
    const anchor = anchorRef.current;
    if (shiftKey && anchor !== null && anchor !== page) {
      onChange(extendSelection(selected, anchor, page, isSelectable));
      return;
    }
    const next = new Set(selected);
    if (next.has(page)) next.delete(page);
    else next.add(page);
    onChange(next);
    anchorRef.current = page;
  };

  // 范围输入 = 整段替换选择（失败页/未完成页自动落选，底栏会写明将排除几页）。
  const applyRange = () => {
    const picked = parsePageRangeInput(rangeText, status.page_count);
    if (picked.length === 0) return;
    onChange(new Set(picked.filter(isSelectable)));
  };

  const stateLabel = (state: PageCellState) => {
    if (state === 'success') return t('batchWizard.pages.stateSuccess');
    if (state === 'failed') return t('batchWizard.pages.stateFailed');
    return t('batchWizard.pages.statePending');
  };

  // Issue#279：卷头路由摘要——docline 接管 / 逐页 + 首个 fallback 码人话；无证据不出 chip。
  const routeChip = (() => {
    const summary = status.route_summary;
    if (!summary) return null;
    if (summary.verdict === 'docline') {
      return (
        <span
          className="inline-flex items-center rounded-full border border-[var(--success-border)] bg-[var(--success-surface)] px-2 py-0.5 text-xs text-[var(--success-foreground)]"
          data-testid="page-route-chip"
        >
          {t('pageDetail.chipDocline')}
        </span>
      );
    }
    return (
      <span
        className="inline-flex items-center rounded-full border border-border bg-muted px-2 py-0.5 text-xs text-muted-foreground"
        data-testid="page-route-chip"
      >
        {t('pageDetail.chipFallback').replace(
          '{reason}',
          humanizeFallback(summary.fallback_reason_code || ''),
        )}
      </span>
    );
  })();

  // Issue#279：展开页的详情投影；页记录缺 detail（旧数据）时字段回落，不空转崩。
  const expandedCell = expandedPage === null ? null : status.pages.find((cell) => cell.page === expandedPage) ?? null;

  return (
    <div className="flex flex-col gap-2" data-testid="page-matrix">
      {routeChip && <div className="flex items-center">{routeChip}</div>}
      <div className="flex flex-wrap items-center gap-1.5">
        <label className="text-xs text-muted-foreground" htmlFor="page-range-input">
          {t('batchWizard.pages.rangeLabel')}
        </label>
        <input
          id="page-range-input"
          type="text"
          inputMode="numeric"
          className="h-7 w-44 rounded-md border border-border bg-background px-2 text-xs tabular-nums"
          placeholder={t('batchWizard.pages.rangePlaceholder')}
          value={rangeText}
          disabled={disabled}
          onChange={(event) => setRangeText(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              applyRange();
            }
          }}
          data-testid="page-matrix-range-input"
        />
        <Button
          type="button"
          size="sm"
          variant="outline"
          className="h-7 px-2 text-xs"
          disabled={disabled || rangeText.trim() === ''}
          onClick={applyRange}
          data-testid="page-matrix-range-apply"
        >
          {t('batchWizard.pages.rangeApply')}
        </Button>
        <span className="text-xs text-muted-foreground" data-testid="page-matrix-shift-hint">
          {t('batchWizard.pages.shiftHint')}
        </span>
      </div>
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
            disabled={disabled || allSuccess.size === 0}
            onClick={() => {
              const next = new Set<number>();
              allSuccess.forEach((page) => {
                if (!selected.has(page)) next.add(page);
              });
              onChange(next);
            }}
            data-testid="page-matrix-invert"
          >
            {t('batchWizard.pages.invert')}
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
                  // Issue#289：三种格子都可点（展开详情）——光标只区分「可用/禁用」，
                  // 不再对失败页谎报不可点；禁用时唯一 cursor 语义，双类不并存。
                  disabled ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
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
                // Issue#289：点击展开对三种状态都有意义，aria-disabled 只跟随 matrix 禁用位。
                aria-disabled={disabled}
                data-testid={`page-cell-${cell.page}`}
                data-state={cell.state}
                data-selected={isSelected ? 'true' : 'false'}
                data-retryable={cell.state === 'failed' && retryable ? 'true' : 'false'}
                data-expanded={expandedPage === cell.page ? 'true' : 'false'}
                aria-expanded={expandedPage === cell.page ? 'true' : 'false'}
                onClick={(event) => activate(cell.page, cell.state, event.shiftKey)}
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

      {expandedCell && (
        <div
          className="rounded-lg border border-border/70 bg-muted/40 p-2 text-xs"
          data-testid="page-detail-panel"
          data-page={expandedCell.page}
        >
          <div className="flex items-center justify-between gap-2">
            <span className="font-medium">
              {t('pageDetail.title').replace('{page}', String(expandedCell.page))}
            </span>
            <Button
              type="button"
              size="sm"
              variant="ghost"
              className="h-6 px-1.5 text-xs"
              onClick={() => setExpandedPage(null)}
              data-testid="page-detail-close"
            >
              {t('pageDetail.close')}
            </Button>
          </div>
          <div className="mt-1 flex flex-col gap-0.5" data-testid="page-detail-body">
            <span data-testid="page-detail-route">
              {t('pageDetail.route')}
              {'：'}
              {expandedCell.detail?.route_verdict === 'docline'
                ? t('pageDetail.routeDocline')
                : t('pageDetail.routePagePipeline')}
              {expandedCell.detail?.fallback_reason_code
                ? `（${humanizeFallback(expandedCell.detail.fallback_reason_code)}）`
                : ''}
            </span>
            {(expandedCell.duration_ms != null || expandedCell.detail?.duration_ms != null) && (
              <span data-testid="page-detail-duration">
                {t('pageDetail.duration').replace(
                  '{ms}',
                  String(expandedCell.duration_ms ?? expandedCell.detail?.duration_ms ?? 0),
                )}
              </span>
            )}
            {expandedCell.detail?.stage_ms && Object.keys(expandedCell.detail.stage_ms).length > 0 && (
              <span data-testid="page-detail-stage-ms">
                {t('pageDetail.stageMs')}
                {'：'}
                {Object.entries(expandedCell.detail.stage_ms)
                  .map(([stage, ms]) => `${humanizeStage(stage)} ${ms}ms`)
                  .join(' / ')}
              </span>
            )}
            {expandedCell.detail?.error_type && (
              <span className="text-destructive" data-testid="page-detail-error-type">
                {t('pageDetail.errorType')}
                {'：'}
                {expandedCell.detail.error_type}
              </span>
            )}
            <span data-testid="page-detail-attempts">
              {(expandedCell.detail?.attempts ?? 1) > 1
                ? t('pageDetail.attempts').replace('{count}', String(expandedCell.detail?.attempts ?? 1))
                : t('pageDetail.noRetry')}
            </span>
          </div>
          {/* Issue#289：重试只走这里的显式按钮（四态=可重试/重试中/识别中忙/已终态），格子点击不触发。 */}
          {expandedCell.state === 'failed' && onRetryPage && (
            <div className="mt-1.5 flex items-center gap-2">
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="h-6 px-2 text-xs"
                disabled={!retryable}
                onClick={() => onRetryPage(expandedCell.page)}
                data-testid="page-detail-retry"
              >
                {retrying ? t('batchWizard.pages.retrying') : t('pageDetail.retryThisPage')}
              </Button>
              {!retryable && !retrying && (
                <span
                  className="text-muted-foreground"
                  data-testid="page-detail-retry-unavailable"
                >
                  {busy
                    ? t('batchWizard.pages.retryUnavailable')
                    : t('pageDetail.retryUnavailableTerminal')}
                </span>
              )}
            </div>
          )}
        </div>
      )}

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
          {/* Issue#289 F4：与面板按钮同口径——忙态/重试在飞与终态的禁用原因文案不混用。
              门⑥ M2：重试在飞 + 快照仍 awaiting_review 的残窗里落「等待当前重试结束」
              措辞，不与同屏按钮「重试中…」矛盾。 */}
          {retryable
            ? t('batchWizard.pages.retryHint')
            : retrying || busy
              ? t('batchWizard.pages.retryUnavailable')
              : t('pageDetail.retryUnavailableTerminal')}
        </p>
      )}
    </div>
  );
}