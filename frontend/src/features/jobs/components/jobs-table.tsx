// Copyright 2026 LexMask Contributors

import {
  memo,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
} from 'react';
import { ArrowRight, Eye, SlidersHorizontal, Trash2 } from 'lucide-react';
import { Link } from 'react-router-dom';
import { t, useI18n, useT } from '@/i18n';
import { useUiTheme } from '@/theme/ui-theme';
import { cn } from '@/lib/utils';
import { EmptyState } from '@/components/EmptyState';
import { AsyncListShell } from '@/components/shared/AsyncListShell';
import {
  clampPageSize,
  interpolateDensity,
  useProportionalRowHeight,
} from '@/components/hooks/useProportionalRowHeight';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import type { JobSummary } from '@/services/jobsApi';
import type { JobsStatusFilter } from '../hooks/use-jobs';
import {
  buildJobPrimaryNavigationLabels,
  resolveJobRowNavigation,
} from '@/utils/jobPrimaryNavigation';
import { JobStatusBadge, JobTypeBadge } from './jobs-status-badge';
import { canDeleteJob } from '../hooks/use-jobs';

function getJobCounts(job: JobSummary): {
  total: number;
  completed: number;
  awaitingReview: number;
  abnormal: number;
} {
  return {
    total: job.nav_hints?.item_count ?? job.progress.total_items,
    completed: job.progress.completed,
    awaitingReview: job.progress.awaiting_review + job.progress.review_approved,
    abnormal: job.progress.failed + (job.progress.cancelled ?? 0),
  };
}

function getProgressMeter(job: JobSummary): {
  percent: number;
  tone: 'brand' | 'success' | 'danger' | 'warning';
} {
  const counts = getJobCounts(job);
  const activeCount =
    job.progress.pending +
    job.progress.queued +
    job.progress.processing +
    job.progress.parsing +
    job.progress.ner +
    job.progress.vision +
    job.progress.redacting;
  const percent =
    counts.total > 0
      ? Math.min(
          100,
          Math.round(
            ((counts.completed + counts.awaitingReview + counts.abnormal) / counts.total) * 100,
          ),
        )
      : 0;

  if (counts.abnormal > 0) return { percent, tone: 'danger' };
  if (counts.awaitingReview > 0) return { percent, tone: 'warning' };
  if (activeCount > 0) return { percent, tone: 'brand' };
  if (counts.total > 0 && counts.completed >= counts.total) {
    return { percent: 100, tone: 'success' };
  }
  return { percent, tone: 'brand' };
}

function formatUpdatedAt(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t('jobs.updatedAtUnknown');
  const locale = useI18n.getState().locale;
  return date.toLocaleString(locale === 'en' ? 'en-US' : 'zh-CN');
}

// Issue#355 ds 表格：人性化时间（定稿「今天 10:42 / 昨天 18:32 / 11-14 11:46」）。
// classic 沿用 formatUpdatedAt（完整本地化时间），双轨互不影响。
function formatUpdatedAtHuman(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return t('jobs.updatedAtUnknown');
  const locale = useI18n.getState().locale === 'en' ? 'en' : 'zh';
  const now = new Date();
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  const hm = date.toLocaleTimeString(locale === 'en' ? 'en-US' : 'zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  if (dayDiff === 0) return locale === 'en' ? `Today ${hm}` : `今天 ${hm}`;
  if (dayDiff === 1) return locale === 'en' ? `Yesterday ${hm}` : `昨天 ${hm}`;
  const md = `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  return `${md} ${hm}`;
}

// Issue#351 B2：空态分流所需的全局口径（工单单位，与表体行数可对账）。
export type JobsTableStats = {
  totalJobs: number;
  activeJobs: number;
  awaitingReviewJobs: number;
  completedJobs: number;
  riskJobs: number;
};

type JobsTableProps = {
  rows: JobSummary[];
  loading: boolean;
  refreshing: boolean;
  tableLoading?: boolean;
  pageSize: number;
  deletingJobId: string | null;
  tableBusy: boolean;
  onDelete: (job: JobSummary) => void;
  tab?: JobsStatusFilter;
  onTabChange?: (tab: JobsStatusFilter) => void;
  stats?: JobsTableStats;
};

const jobsGridStyle: CSSProperties = {
  gridTemplateColumns:
    'minmax(210px,1.16fr) minmax(44px,0.24fr) minmax(54px,0.28fr) minmax(54px,0.28fr) minmax(50px,0.26fr) minmax(155px,0.82fr) minmax(96px,0.48fr) minmax(124px,0.58fr) minmax(72px,0.36fr) minmax(72px,0.36fr) minmax(60px,0.3fr)',
  columnGap: '10px',
};

// Issue#355 ds 骨架（定稿表格）：任务/状态/进度/统计/更新时间/3 操作 —— 8 列。
// 4 个裸数字计数列合并为「已完成/总数 + 状态角标」统计列（定稿 §6.4）。
const dsJobsGridStyle: CSSProperties = {
  gridTemplateColumns:
    'minmax(220px,2.1fr) minmax(92px,0.55fr) minmax(168px,1.05fr) minmax(92px,0.5fr) minmax(136px,0.7fr) minmax(44px,0.3fr) minmax(44px,0.3fr) minmax(44px,0.3fr)',
  columnGap: '14px',
};

function stopRowClick(event: ReactMouseEvent) {
  event.stopPropagation();
}

const JOBS_MIN_PAGE_SIZE = 10;
const JOBS_MAX_PAGE_SIZE = 20;
// Issue#355：ds 表格行高下限（定稿 56px，副信息行需要竖向空间）；classic 走密度系统。
const JOBS_DS_ROW_MIN_HEIGHT = 56;
const JOBS_TABLE_MIN_CHILD_HEIGHT = 22;
const JOBS_TABLE_MAX_CHILD_HEIGHT = 34;
const JOBS_TABLE_MIN_PADDING_Y = 3;
const JOBS_TABLE_MAX_PADDING_Y = 8;
const JOBS_TABLE_MIN_CHILD_PADDING_Y = 2;
const JOBS_TABLE_MAX_CHILD_PADDING_Y = 6;
const JOB_STATUS_FILTERS: JobsStatusFilter[] = [
  'all',
  'active',
  'awaiting_review',
  'completed',
  'risk',
  'draft',
];

type JobsTableDensity = {
  rowHeight: number;
  childRowHeight: number;
  rowPaddingY: number;
  childRowPaddingY: number;
  skeletonHeight: number;
};

function normalizeJobsPageSize(pageSize: number): number {
  return clampPageSize(pageSize, JOBS_MIN_PAGE_SIZE, JOBS_MAX_PAGE_SIZE);
}

function getJobsTableDensity(pageSize: number, rowHeight: number): JobsTableDensity {
  const interpolate = interpolateDensity(pageSize, JOBS_MIN_PAGE_SIZE, JOBS_MAX_PAGE_SIZE);

  return {
    rowHeight,
    childRowHeight: Math.max(
      10,
      Math.min(
        interpolate(JOBS_TABLE_MAX_CHILD_HEIGHT, JOBS_TABLE_MIN_CHILD_HEIGHT),
        rowHeight * 0.8,
      ),
    ),
    rowPaddingY: Math.max(
      0,
      Math.min(interpolate(JOBS_TABLE_MAX_PADDING_Y, JOBS_TABLE_MIN_PADDING_Y), rowHeight * 0.08),
    ),
    childRowPaddingY: interpolate(JOBS_TABLE_MAX_CHILD_PADDING_Y, JOBS_TABLE_MIN_CHILD_PADDING_Y),
    skeletonHeight: Math.max(8, rowHeight - 4),
  };
}

function getJobsSkeletonCount(pageSize: number): number {
  return normalizeJobsPageSize(pageSize);
}

export function JobsTable({
  rows,
  loading,
  refreshing,
  tableLoading = false,
  pageSize,
  deletingJobId,
  tableBusy: _tableBusy,
  onDelete,
  tab,
  onTabChange,
  stats,
}: JobsTableProps) {
  const t = useT();
  const bodyRef = useRef<HTMLDivElement>(null);
  // Issue#355 ds 骨架：定稿表格（8 列 + 合并统计列 + 副信息行 + 人性化时间 + 56px 行高下限）。
  const dsMode = useUiTheme((s) => s.theme) === 'ds';
  const rowHeight = useProportionalRowHeight({
    pageSize,
    minPageSize: JOBS_MIN_PAGE_SIZE,
    maxPageSize: JOBS_MAX_PAGE_SIZE,
    bodyRef,
  });
  // ds 下行高不低于定稿 56px（副信息行需要竖向空间）；classic 密度系统原样。
  const effectiveRowHeight = dsMode ? Math.max(rowHeight, JOBS_DS_ROW_MIN_HEIGHT) : rowHeight;
  const density = useMemo(
    () => getJobsTableDensity(pageSize, effectiveRowHeight),
    [pageSize, effectiveRowHeight],
  );
  const safePageSize = normalizeJobsPageSize(pageSize);
  const fillerRowCount = Math.max(0, safePageSize - rows.length);
  const bodyStyle: CSSProperties = {
    height: 0,
    minHeight: 0,
    overscrollBehavior: 'contain',
    scrollbarGutter: 'stable',
  };
  const hardLoading = loading && rows.length === 0;
  const showJobType = rows.length > 0 && rows.some((job) => job.job_type !== rows[0].job_type);

  // Issue#351 B2：空态分流。页签徽标/统计卡为全局口径、表体按 tab 过滤——过滤态
  // 0 行但其他状态有任务时，空态必须告知数量并给出去处（修「徽标 7 vs 空表」困惑）。
  // 数字口径=工单（与落地行数可对账）；Number.isFinite 兼容未传全字段的旧调用方。
  const isFilteredTab = Boolean(tab) && tab !== 'all';
  const finite = (n: number | undefined): n is number =>
    typeof n === 'number' && Number.isFinite(n);
  const emptyParts: string[] = [];
  if (stats && finite(stats.totalJobs) && stats.totalJobs > 0 && isFilteredTab) {
    const partDefs = [
      { include: tab !== 'active', n: stats.activeJobs, key: 'jobs.emptyPart.active' },
      { include: tab !== 'awaiting_review', n: stats.awaitingReviewJobs, key: 'jobs.emptyPart.review' },
      { include: tab !== 'completed', n: stats.completedJobs, key: 'jobs.emptyPart.completed' },
      { include: tab !== 'risk', n: stats.riskJobs, key: 'jobs.emptyPart.risk' },
    ] as const;
    for (const def of partDefs) {
      if (def.include && finite(def.n) && def.n > 0) {
        emptyParts.push(t(def.key).replace('{n}', String(def.n)));
      }
    }
  }
  const showFilteredEmpty = rows.length === 0 && emptyParts.length > 0;
  const canGoReview =
    showFilteredEmpty &&
    Boolean(onTabChange) &&
    tab !== 'awaiting_review' &&
    finite(stats?.awaitingReviewJobs) &&
    (stats?.awaitingReviewJobs ?? 0) > 0;

  return (
    <div
      className="jobs-surface page-surface flex min-h-0 flex-1 flex-col overflow-hidden w-full rounded-[20px] border border-border/70 bg-card/95 shadow-[var(--shadow-md)]"
      data-testid="jobs-table-surface"
      aria-busy={loading || refreshing || tableLoading}
    >
      <div className="flex shrink-0 flex-nowrap items-center justify-between gap-3 border-b border-border/70 px-3 py-2.5 sm:px-4">
        <div className="page-section-heading min-w-0">
          <h3 className="jobs-table-heading truncate text-sm font-semibold tracking-[-0.02em]">
            {t('jobs.taskRecords')}
          </h3>
        </div>
        {tab && onTabChange ? (
          <JobsFilterMenu tab={tab} onTabChange={onTabChange} />
        ) : null}
      </div>

      <div
        className="jobs-table-head shrink-0 border-b border-border/70 bg-muted/40 px-3 py-2 text-xs font-medium text-muted-foreground sm:px-4"
        style={dsMode ? dsJobsGridStyle : jobsGridStyle}
      >
        {dsMode ? (
          <>
            <span className="jobs-task-cell">{t('jobs.task')}</span>
            <span className="jobs-status-cell">{t('jobs.currentStatus')}</span>
            <span className="jobs-progress-cell">{t('jobs.progress')}</span>
            <span className="jobs-count-head">{t('jobs.statsHeader')}</span>
            <span className="jobs-updated-cell">{t('jobs.updatedAt')}</span>
            <span className="jobs-action-column-head">{t('jobNav.continueReview')}</span>
            <span className="jobs-action-column-head">{t('jobs.viewDetail')}</span>
            <span className="jobs-action-column-head">{t('jobs.deleteAction')}</span>
          </>
        ) : (
          <>
            <span className="jobs-task-cell">{t('jobs.task')}</span>
            <span className="jobs-count-head">{t('jobs.totalCountHeader')}</span>
            <span className="jobs-count-head">{t('jobs.completedHeader')}</span>
            <span className="jobs-count-head">{t('jobs.awaitingReviewHeader')}</span>
            <span className="jobs-count-head">{t('jobs.abnormalHeader')}</span>
            <span className="jobs-progress-cell">{t('jobs.progress')}</span>
            <span className="jobs-status-cell">{t('jobs.currentStatus')}</span>
            <span className="jobs-updated-cell">{t('jobs.updatedAt')}</span>
            <span className="jobs-action-column-head">{t('jobNav.continueReview')}</span>
            <span className="jobs-action-column-head">{t('jobs.viewDetail')}</span>
            <span className="jobs-action-column-head">{t('jobs.deleteAction')}</span>
          </>
        )}
      </div>

      <div
        className="jobs-table-body page-surface-body relative flex min-h-0 flex-1 flex-col overflow-x-auto overflow-y-auto"
        ref={bodyRef}
        style={bodyStyle}
        data-testid="jobs-table-body"
      >
        <AsyncListShell
          hardLoading={hardLoading}
          isEmpty={rows.length === 0}
          className="min-h-full"
          skeleton={
            <div className="flex flex-col gap-2 px-4 py-4 pb-7">
              {Array.from({ length: getJobsSkeletonCount(pageSize) }).map((_, i) => (
                <Skeleton
                  key={i}
                  className="w-full rounded-lg"
                  style={{ height: `${density.skeletonHeight}px` }}
                />
              ))}
            </div>
          }
          emptyContent={
            showFilteredEmpty ? (
              <div
                className="flex min-h-full items-center justify-center px-4"
                data-testid="jobs-table-empty-filtered"
              >
                <EmptyState
                  title={t('jobs.emptyFiltered')}
                  description={t('jobs.emptyFilteredHint').replace('{parts}', emptyParts.join(' · '))}
                  action={
                    canGoReview
                      ? {
                          label: t('jobs.emptyGoReview'),
                          onClick: () => onTabChange?.('awaiting_review'),
                        }
                      : undefined
                  }
                />
              </div>
            ) : (
              <div
                className="flex min-h-full items-center justify-center px-4"
                data-testid="jobs-table-empty"
              >
                <EmptyState
                  title={t('jobs.noRecords')}
                  description={t('jobs.noRecordsHint')}
                  action={{
                    label: t('jobs.gotoBatch'),
                    onClick: () => {
                      window.location.href = '/batch';
                    },
                  }}
                />
              </div>
            )
          }
        >
          <ul className="jobs-table-list flex min-h-full min-w-full flex-col divide-y divide-border/70">
            {rows.map((job, index) => (
              <JobRow
                key={job.id}
                job={job}
                index={index}
                deletingJobId={deletingJobId}
                density={density}
                showJobType={showJobType}
                onDelete={onDelete}
                stopEvent={stopRowClick}
                dsMode={dsMode}
              />
            ))}
            {Array.from({ length: fillerRowCount }).map((_, index) => (
              <li
                key={`jobs-filler-${index}`}
                className="shrink-0 bg-background"
                style={{ height: `${density.rowHeight}px`, minHeight: `${density.rowHeight}px` }}
                aria-hidden
              />
            ))}
          </ul>
        </AsyncListShell>
      </div>
    </div>
  );
}

function getJobsTabLabel(t: (key: string) => string, value: JobsStatusFilter): string {
  if (value === 'all') return t('jobs.tab.all');
  if (value === 'active') return t('jobs.filter.active');
  if (value === 'awaiting_review') return t('jobs.filter.awaitingReview');
  if (value === 'completed') return t('jobs.filter.completed');
  if (value === 'risk') return t('jobs.filter.risk');
  return t('jobs.filter.draft');
}

function JobsFilterMenu({
  tab,
  onTabChange,
}: {
  tab: JobsStatusFilter;
  onTabChange: (tab: JobsStatusFilter) => void;
}) {
  const t = useT();
  const [filterOpen, setFilterOpen] = useState(false);
  const filterRef = useRef<HTMLDivElement>(null);
  const hasActiveFilter = tab !== 'all';
  const filterTabsListClass = 'h-8 w-full rounded-lg border border-border/70 bg-muted/55 p-0.5';
  const filterTabClass =
    'flex-1 rounded-md border border-transparent px-2 py-1 text-xs text-muted-foreground transition-colors data-[state=active]:bg-foreground data-[state=active]:font-semibold data-[state=active]:text-background data-[state=active]:shadow-sm data-[state=active]:ring-1 data-[state=active]:ring-inset data-[state=active]:ring-foreground/45';

  useEffect(() => {
    if (!filterOpen) return;

    const closeOnOutsideClick = (event: globalThis.MouseEvent) => {
      if (!filterRef.current?.contains(event.target as Node)) {
        setFilterOpen(false);
      }
    };

    document.addEventListener('mousedown', closeOnOutsideClick);
    return () => document.removeEventListener('mousedown', closeOnOutsideClick);
  }, [filterOpen]);

  return (
    <div className="relative flex shrink-0 flex-nowrap items-center gap-1.5" ref={filterRef}>
      <Button
        variant={hasActiveFilter ? 'default' : 'outline'}
        size="sm"
        className="h-8 w-16 shrink-0 rounded-lg px-2 text-xs whitespace-nowrap"
        onClick={() => setFilterOpen((open) => !open)}
        data-testid="jobs-filter-menu"
        aria-expanded={filterOpen}
      >
        <SlidersHorizontal data-icon="inline-start" />
        {t('history.filters.kicker')}
      </Button>

      <div
        className="absolute right-0 top-9 z-[80] w-[390px] rounded-xl border border-border bg-popover p-3 shadow-[var(--shadow-lg)]"
        hidden={!filterOpen}
        data-testid="jobs-filter-popover"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <div className="grid grid-cols-[3rem_minmax(0,1fr)] items-center gap-2">
          <div className="truncate text-[11px] font-medium text-muted-foreground">
            {t('jobs.statusLabel')}
          </div>
          <Tabs
            value={tab}
            onValueChange={(value) => onTabChange(value as JobsStatusFilter)}
            data-testid="jobs-status-filter"
          >
            <TabsList className={filterTabsListClass} data-testid="jobs-table-tab-list">
              {JOB_STATUS_FILTERS.map((value) => (
                <TabsTrigger
                  key={value}
                  value={value}
                  className={filterTabClass}
                  data-testid={`jobs-tab-${value}`}
                >
                  {getJobsTabLabel(t, value)}
                </TabsTrigger>
              ))}
            </TabsList>
          </Tabs>
        </div>

        {hasActiveFilter && (
          <div className="mt-2 flex justify-end border-t border-border/70 pt-2">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 rounded-lg px-2 text-xs whitespace-nowrap"
              onClick={() => onTabChange('all')}
              data-testid="clear-jobs-filter"
            >
              {t('history.clearFilter')}
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

type JobRowProps = {
  job: JobSummary;
  index: number;
  deletingJobId: string | null;
  density: JobsTableDensity;
  showJobType: boolean;
  onDelete: (job: JobSummary) => void;
  stopEvent: (e: ReactMouseEvent) => void;
  dsMode: boolean;
};

function MetricCell({
  value,
  title,
  testId,
  tone = 'default',
}: {
  value: number;
  title: string;
  testId: string;
  tone?: 'default' | 'muted' | 'danger';
}) {
  return (
    <div className="jobs-metric-cell" title={`${title}: ${value}`} data-testid={testId}>
      <span
        className={cn(
          'jobs-metric-value',
          tone === 'muted' && 'text-muted-foreground/55',
          tone === 'danger' && 'text-[var(--error-foreground)]',
        )}
      >
        {value}
      </span>
    </div>
  );
}

const JobRow = memo(function JobRow({
  job,
  index,
  deletingJobId,
  density,
  showJobType,
  onDelete,
  stopEvent,
  dsMode,
}: JobRowProps) {
  const t = useT();
  const navLabels = buildJobPrimaryNavigationLabels(t);
  // T12：single 行分流识别页（/single?file_id=），批量四类型沿用既有解析（零漂移）。
  const primary = resolveJobRowNavigation(job, navLabels);
  const detailHref = `/jobs/${encodeURIComponent(job.id)}`;
  // single 行所有状态都出直达按钮（识别页接管状态视图）；批量行保持只对
  // 「继续复审」出按钮的既有口径。
  const showPrimaryAction =
    primary.kind === 'link' &&
    primary.to !== detailHref &&
    (job.job_type === 'single' || primary.label === navLabels.continueReview);
  const deleteBlocked = !canDeleteJob(job.status);
  const stripe = index % 2 === 1 ? 'bg-muted/30' : 'bg-background';
  const counts = getJobCounts(job);
  const progressMeter = getProgressMeter(job);

  const compactRow = density.rowHeight < 36;
  const actionIconBtnBase = cn(
    'size-7 rounded-lg shadow-none hover:translate-y-0',
    compactRow && 'size-6',
  );
  const actionPlaceholder = (
    <span
      className={cn('jobs-action-placeholder rounded-lg', compactRow ? '!min-h-6' : '!min-h-7')}
    />
  );

  return (
    <li className="shrink-0">
      <div
        className={cn(stripe, 'transition-colors hover:bg-muted/30')}
        data-testid={`job-row-${job.id}`}
      >
        <div
          className="jobs-row-main overflow-hidden whitespace-nowrap px-3 py-2 sm:px-4"
          style={{
            ...(dsMode ? dsJobsGridStyle : jobsGridStyle),
            height: `${density.rowHeight}px`,
            minHeight: `${density.rowHeight}px`,
            paddingTop: `${density.rowPaddingY}px`,
            paddingBottom: `${density.rowPaddingY}px`,
          }}
        >
          {dsMode ? (
            <>
              {/* ds 任务列：类型徽章 + 标题 + 副信息行（定稿 .tname/.tmeta） */}
              <div className="jobs-task-cell min-w-0 overflow-hidden">
                <div className="flex min-w-0 flex-col gap-0.5">
                  <div className="flex min-w-0 flex-nowrap items-center gap-2">
                    {showJobType ? <JobTypeBadge jobType={job.job_type} /> : null}
                    <p
                      className="truncate text-sm font-medium"
                      title={job.title || t('jobs.unnamedTask')}
                    >
                      {job.title || t('jobs.unnamedTask')}
                    </p>
                  </div>
                  <p className="truncate text-[11px] leading-tight text-muted-foreground">
                    {t('jobs.filesCount').replace('{n}', String(counts.total))}
                  </p>
                </div>
              </div>

              <div className="jobs-status-cell flex min-w-0 flex-nowrap items-center gap-2">
                <span className="shrink-0 text-xs text-muted-foreground md:hidden">
                  {t('jobs.currentStatus')}
                </span>
                <JobStatusBadge status={job.status} />
              </div>

              <div className="jobs-progress-cell min-w-0 overflow-hidden">
                <div className="flex min-w-0 flex-nowrap items-center gap-2 whitespace-nowrap">
                  <div className="h-1.5 min-w-[72px] flex-1 overflow-hidden rounded-full bg-muted">
                    <div
                      className={cn(
                        'h-full rounded-full transition-all',
                        progressMeter.tone === 'danger'
                          ? 'tone-progress-danger'
                          : progressMeter.tone === 'success'
                            ? 'tone-progress-success'
                            : progressMeter.tone === 'warning'
                              ? 'tone-progress-warning'
                              : 'tone-progress-brand',
                      )}
                      style={{ width: `${progressMeter.percent}%` }}
                    />
                  </div>
                  <span
                    className="shrink-0 text-caption text-muted-foreground tabular-nums"
                    data-testid={`job-progress-state-${job.id}`}
                  >
                    {progressMeter.percent}%
                  </span>
                </div>
              </div>

              {/* ds 统计列（定稿 .counts）：已完成/总数 + 待审/失败角标 */}
              <div
                className="jobs-metric-cell gap-1.5"
                data-testid={`job-stats-${job.id}`}
                title={`${t('jobs.completedHeader')} / ${t('jobs.totalCountHeader')}`}
              >
                <span className="jobs-metric-value">
                  {counts.completed} / {counts.total}
                </span>
                {counts.awaitingReview > 0 && (
                  <span className="tone-badge-review shrink-0 whitespace-nowrap rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums">
                    {t('jobs.awaitingBadge').replace('{n}', String(counts.awaitingReview))}
                  </span>
                )}
                {counts.abnormal > 0 && (
                  <span className="tone-badge-danger shrink-0 whitespace-nowrap rounded-full px-1.5 py-0.5 text-[10px] font-semibold tabular-nums">
                    {t('jobs.failedBadge').replace('{n}', String(counts.abnormal))}
                  </span>
                )}
              </div>

              <div className="jobs-updated-cell hidden text-caption text-muted-foreground tabular-nums whitespace-nowrap md:block">
                {formatUpdatedAtHuman(job.updated_at)}
              </div>
            </>
          ) : (
            <>
              <div className="jobs-task-cell min-w-0 overflow-hidden">
                <div className="flex min-w-0 flex-nowrap items-center gap-2">
                  {showJobType ? <JobTypeBadge jobType={job.job_type} /> : null}
                  <p
                    className="truncate text-sm font-medium"
                    title={job.title || t('jobs.unnamedTask')}
                  >
                    {job.title || t('jobs.unnamedTask')}
                  </p>
                </div>
              </div>

              <MetricCell
                value={counts.total}
                title={t('jobs.totalCountHeader')}
                testId={`job-total-count-${job.id}`}
              />
              <MetricCell
                value={counts.completed}
                title={t('jobs.completedHeader')}
                testId={`job-completed-count-${job.id}`}
              />
              <MetricCell
                value={counts.awaitingReview}
                title={t('jobs.awaitingReviewHeader')}
                testId={`job-awaiting-review-count-${job.id}`}
              />
              <MetricCell
                value={counts.abnormal}
                title={t('jobs.abnormalHeader')}
                tone={counts.abnormal > 0 ? 'danger' : 'muted'}
                testId={`job-abnormal-count-${job.id}`}
              />

          <div className="jobs-progress-cell min-w-0 overflow-hidden">
            <div className="flex min-w-0 flex-nowrap items-center gap-2 whitespace-nowrap">
              <div className="h-1.5 min-w-[72px] flex-1 overflow-hidden rounded-full bg-muted">
                <div
                  className={cn(
                    'h-full rounded-full transition-all',
                    progressMeter.tone === 'danger'
                      ? 'tone-progress-danger'
                      : progressMeter.tone === 'success'
                        ? 'tone-progress-success'
                        : progressMeter.tone === 'warning'
                          ? 'tone-progress-warning'
                          : 'tone-progress-brand',
                  )}
                  style={{ width: `${progressMeter.percent}%` }}
                />
              </div>
              <span
                className="shrink-0 text-caption text-muted-foreground tabular-nums"
                data-testid={`job-progress-state-${job.id}`}
              >
                {progressMeter.percent}%
              </span>
            </div>
          </div>

          <div className="jobs-status-cell flex min-w-0 flex-nowrap items-center gap-2">
            <span className="shrink-0 text-xs text-muted-foreground md:hidden">
              {t('jobs.currentStatus')}
            </span>
            <JobStatusBadge status={job.status} />
          </div>

          <div className="jobs-updated-cell hidden md:block text-caption text-muted-foreground tabular-nums whitespace-nowrap">
            {formatUpdatedAt(job.updated_at)}
          </div>
            </>
          )}

          <div className="jobs-action-cell" onClick={stopEvent}>
            {showPrimaryAction && primary.kind === 'link' ? (
              <Button
                asChild
                variant="outline"
                size="icon"
                className={cn(actionIconBtnBase, 'bg-background hover:bg-muted')}
              >
                <Link
                  to={primary.to}
                  onClick={stopEvent}
                  title={primary.label}
                  aria-label={primary.label}
                  data-testid={`job-primary-action-${job.id}`}
                >
                  <ArrowRight data-icon="inline-end" />
                  <span className="sr-only">{primary.label}</span>
                </Link>
              </Button>
            ) : (
              actionPlaceholder
            )}
          </div>
          <div className="jobs-action-cell" onClick={stopEvent}>
            <Button
              asChild
              variant="outline"
              size="icon"
              className={cn(actionIconBtnBase, 'bg-background hover:bg-muted')}
            >
              <Link
                to={detailHref}
                onClick={stopEvent}
                title={t('jobs.viewDetail')}
                aria-label={t('jobs.viewDetail')}
                data-testid={`job-detail-link-${job.id}`}
              >
                <Eye data-icon="inline-start" />
                <span className="sr-only">{t('jobs.viewDetail')}</span>
              </Link>
            </Button>
          </div>
          <div className="jobs-action-cell" onClick={stopEvent}>
            {!deleteBlocked ? (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                disabled={deletingJobId === job.id}
                onClick={(e) => {
                  e.stopPropagation();
                  void onDelete(job);
                }}
                className={cn(
                  actionIconBtnBase,
                  'border border-transparent text-muted-foreground hover:border-[var(--error-border)] hover:bg-[var(--error-surface)] hover:text-[var(--error-foreground)] disabled:opacity-50',
                )}
                title={t('jobs.deleteTask')}
                aria-label={t('jobs.deleteTask')}
                data-testid={`job-delete-${job.id}`}
              >
                <Trash2
                  data-icon="inline-start"
                  className={cn(deletingJobId === job.id && 'animate-pulse')}
                />
                <span className="sr-only">
                  {deletingJobId === job.id ? t('jobs.deletingEllipsis') : t('jobs.deleteTask')}
                </span>
              </Button>
            ) : (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                disabled
                className={cn(
                  actionIconBtnBase,
                  'border border-transparent text-muted-foreground opacity-35',
                )}
                title={t('jobs.cancelBeforeDelete')}
                aria-label={t('jobs.deleteTask')}
                data-testid={`job-delete-disabled-${job.id}`}
              >
                <Trash2 data-icon="inline-start" />
                <span className="sr-only">{t('jobs.deleteTask')}</span>
              </Button>
            )}
          </div>
        </div>
      </div>
    </li>
  );
});
