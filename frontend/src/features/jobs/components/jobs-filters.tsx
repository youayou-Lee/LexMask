// Copyright 2026 LexMask Contributors

import { RefreshCw, Trash2 } from 'lucide-react';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';

// Issue#351 B3：本组件只消费与页签口径不重叠的两项（其余口径由页签徽标承载）。
type PageMetrics = {
  totalJobs: number;
  riskItems: number;
};

type JobsFiltersProps = {
  onRefresh: () => void;
  onCleanup: () => void;
  refreshing: boolean;
  tableBusy: boolean;
  metrics: PageMetrics;
};

export function JobsFilters({
  onRefresh,
  onCleanup,
  refreshing,
  tableBusy,
  metrics,
}: JobsFiltersProps) {
  const t = useT();

  return (
    <section className="saas-panel mb-2 grid shrink-0 gap-2.5 rounded-[20px] border-border/70 bg-card/95 p-2.5 shadow-[var(--shadow-control)] xl:grid-cols-[minmax(260px,0.7fr)_minmax(520px,1fr)_auto] xl:items-center 2xl:p-3">
      <div className="flex min-w-0 flex-col gap-1">
        {/* Issue#351 B3：删「操作」kicker chip（对用户零信息量） */}
        <h2 className="page-title truncate text-base">{t('jobs.filters.title')}</h2>
        <p className="page-copy truncate text-xs leading-4">{t('jobs.filters.desc')}</p>
      </div>

      {/* Issue#351 B3：统计卡只留与页签徽标口径不重叠的两张——
          运行/待复核/已完成由页签承载（同一信息只出现一次）。 */}
      <div className="grid min-w-0 grid-cols-2 gap-1.5 xl:max-w-[280px]">
        <MetricPill label={t('jobs.metric.totalJobs')} value={metrics.totalJobs} />
        <MetricPill label={t('jobs.metric.riskFiles')} value={metrics.riskItems} tone="alert" />
      </div>

      <div className="control-cluster min-w-0 !flex-nowrap justify-start overflow-x-auto pb-1 xl:justify-end xl:pb-0">
        <Button
          variant="outline"
          size="sm"
          onClick={onRefresh}
          disabled={tableBusy}
          data-testid="jobs-refresh-btn"
          title={t('jobs.refreshTitle')}
          className="h-8 min-w-[5.5rem] shrink-0 justify-center rounded-lg px-2.5 text-xs whitespace-nowrap"
        >
          <RefreshCw data-icon="inline-start" className={refreshing ? 'animate-spin' : ''} />
          {refreshing ? t('jobs.refreshing') : t('jobs.clickRefresh')}
        </Button>

        <Button
          variant="outline"
          size="sm"
          className="h-8 shrink-0 rounded-lg border-destructive/25 px-2.5 text-xs whitespace-nowrap text-destructive hover:bg-destructive/10"
          onClick={onCleanup}
          data-testid="jobs-cleanup-btn"
        >
          <Trash2 data-icon="inline-start" />
          {t('jobs.cleanupButton')}
        </Button>
      </div>
    </section>
  );
}

function MetricPill({
  label,
  value,
  tone = 'default',
}: {
  label: string;
  value: number;
  tone?: 'default' | 'alert';
}) {
  return (
    <div className="min-w-0 rounded-lg border border-border bg-card/80 px-2 py-1 shadow-[var(--shadow-sm)]">
      <div className="truncate text-[10px] uppercase tracking-[0.08em] text-muted-foreground">
        {label}
      </div>
      <div
        className={
          tone === 'alert'
            ? 'truncate text-sm font-semibold leading-4 tabular-nums text-[var(--error-foreground)]'
            : 'truncate text-sm font-semibold leading-4 tabular-nums text-foreground'
        }
      >
        {value}
      </div>
    </div>
  );
}
