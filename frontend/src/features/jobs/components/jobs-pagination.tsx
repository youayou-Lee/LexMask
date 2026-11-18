// Copyright 2026 LexMask Contributors

import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { useUiTheme } from '@/theme/ui-theme';
import { PaginationRail } from '@/components/PaginationRail';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { PAGE_SIZE_OPTIONS } from '../hooks/use-jobs';

type JobsPaginationProps = {
  page: number;
  pageSize: number;
  totalPages: number;
  total: number;
  rangeStart: number;
  rangeEnd: number;
  jumpPage: string;
  tableBusy: boolean;
  onGoPage: (page: number) => void;
  onChangePageSize: (size: number) => void;
  onJumpPageChange: (value: string) => void;
};

export function JobsPagination({
  page,
  pageSize,
  totalPages,
  total,
  rangeStart: _rangeStart,
  rangeEnd: _rangeEnd,
  jumpPage: _jumpPage,
  tableBusy,
  onGoPage,
  onChangePageSize,
  onJumpPageChange: _onJumpPageChange,
}: JobsPaginationProps) {
  const t = useT();
  // Issue#355 iOS 重置：ds 下换装数字页码胶囊分页器（真稿 .pager）；
  // classic 保持 PaginationRail 零漂移。数据口径（page/totalPages/total/pageSize
  // 与 onGoPage/onChangePageSize）完全同源，仅渲染层不同。
  const dsMode = useUiTheme((s) => s.theme) === 'ds';
  if (dsMode) {
    return (
      <NumericPager
        page={page}
        pageSize={pageSize}
        total={total}
        totalPages={totalPages}
        disabled={tableBusy}
        onGoPage={onGoPage}
        onChangePageSize={onChangePageSize}
        pageSizeOptions={PAGE_SIZE_OPTIONS}
        infoText={t('ds.pager.info')
          .replace('{total}', String(total))
          .replace('{size}', String(pageSize))}
      />
    );
  }

  return (
    <PaginationRail
      page={page}
      pageSize={pageSize}
      totalItems={total}
      totalPages={totalPages}
      onPageChange={(nextPage) => {
        if (tableBusy) return;
        onGoPage(nextPage);
      }}
      onPageSizeChange={(size) => {
        if (tableBusy) return;
        onChangePageSize(size);
      }}
      pageSizeOptions={PAGE_SIZE_OPTIONS}
      rangeLabel={t('jobs.showRange')}
      perPageLabel={t('jobs.perPage')}
      itemsUnitLabel={t('jobs.itemsUnit')}
      compact
      disabled={tableBusy}
      reserveWhenEmpty
      className="jobs-pagination-rail !min-h-10 !rounded-xl border-border/70 bg-muted/40"
    />
  );
}

export type NumericPagerProps = {
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
  disabled?: boolean;
  onGoPage: (page: number) => void;
  onChangePageSize?: (size: number) => void;
  pageSizeOptions?: readonly number[];
  /** 左侧信息文案（调用方组好 i18n 占位符再传） */
  infoText?: string;
  perPageLabel?: string;
  itemsUnitLabel?: string;
};

/** 数字页码窗口（1 2 3 … N，含首末页与当前页 ±1；≤7 页全展开）。 */
export function buildNumericPagerItems(page: number, totalPages: number): (number | 'gap')[] {
  const safeTotal = Math.max(1, totalPages);
  if (safeTotal <= 7) {
    return Array.from({ length: safeTotal }, (_, i) => i + 1);
  }
  const wanted = new Set<number>([1, 2, safeTotal - 1, safeTotal, page - 1, page, page + 1]);
  const pages = [...wanted].filter((p) => p >= 1 && p <= safeTotal).sort((a, b) => a - b);
  const items: (number | 'gap')[] = [];
  let prev = 0;
  for (const p of pages) {
    if (p - prev > 1) items.push('gap');
    items.push(p);
    prev = p;
  }
  return items;
}

/**
 * Issue#355 iOS 重置：数字页码胶囊分页器（真稿 ios-tasks-center .pager：
 * 左信息文案 + 右数字页码，当前页=绿底白字胶囊，‹ › 翻页）。
 * ds 分支专用（jobs 任务中心 + history 结果视图）；classic 用 PaginationRail。
 * data-testid 沿用 "pagination-rail"（与 classic 分页器同 testid，页面级锚点不变）。
 */
export function NumericPager({
  page,
  pageSize,
  total,
  totalPages,
  disabled = false,
  onGoPage,
  onChangePageSize,
  pageSizeOptions,
  infoText,
  perPageLabel,
  itemsUnitLabel,
}: NumericPagerProps) {
  const t = useT();
  const safeTotalPages = Math.max(1, totalPages);
  const safePage = Math.min(Math.max(1, page), safeTotalPages);
  const hasItems = total > 0;
  const pageDisabled = disabled || !hasItems;
  const prevDisabled = pageDisabled || safePage <= 1;
  const nextDisabled = pageDisabled || safePage >= safeTotalPages;
  const items = buildNumericPagerItems(safePage, safeTotalPages);
  const perPageUnit = itemsUnitLabel ?? t('jobs.itemsUnit');

  return (
    <div className="ios-pager" data-testid="pagination-rail">
      <div className="ios-pager__meta">
        {infoText ? <span className="ios-pager__info">{infoText}</span> : null}
        {onChangePageSize && pageSizeOptions && pageSizeOptions.length > 0 ? (
          <Select
            value={String(pageSize)}
            disabled={disabled}
            onValueChange={(value) => onChangePageSize(Number(value))}
          >
            <SelectTrigger
              aria-label={perPageLabel ?? t('jobs.perPage')}
              className="ios-pager__size justify-between tabular-nums"
            >
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {pageSizeOptions.map((size) => (
                <SelectItem key={size} value={String(size)}>
                  {`${size} ${perPageUnit}`.trim()}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : null}
      </div>
      <div className="ios-pager__pages">
        <button
          type="button"
          className="ios-pager__pg"
          disabled={prevDisabled}
          onClick={() => onGoPage(safePage - 1)}
          aria-label={t('jobs.prevPage')}
          data-testid="pager-prev"
        >
          <ChevronLeft aria-hidden />
        </button>
        {items.map((item, index) =>
          item === 'gap' ? (
            <span key={`gap-${index}`} className="ios-pager__ellipsis" aria-hidden>
              …
            </span>
          ) : (
            <button
              key={item}
              type="button"
              className={cn('ios-pager__pg', item === safePage && 'ios-pager__pg--on')}
              disabled={pageDisabled}
              aria-current={item === safePage ? 'page' : undefined}
              aria-label={t('ds.pager.pageLabel').replace('{page}', String(item))}
              onClick={() => {
                if (item !== safePage) onGoPage(item);
              }}
            >
              {item}
            </button>
          ),
        )}
        <button
          type="button"
          className="ios-pager__pg"
          disabled={nextDisabled}
          onClick={() => onGoPage(safePage + 1)}
          aria-label={t('jobs.nextPage')}
          data-testid="pager-next"
        >
          <ChevronRight aria-hidden />
        </button>
      </div>
    </div>
  );
}
