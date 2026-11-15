// Copyright 2026 LexMask Contributors

// Issue#311：结果页「详情」弹窗——上半质量报告、下半处理详情。
// 默认视图不挂载报告/详情内容（DOM 里不存在），只有点「详情」才按需 Portal 渲染。
// Radix Dialog 自带 ESC 关闭、遮罩点击关闭与焦点陷阱（键盘可达），不引新依赖。

import { type FC } from 'react';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import {
  ProcessingDetailsCard,
  type PlaygroundProcessingDetailsState,
} from './processing-details-card';

function numberFrom(value: unknown): number {
  const n = typeof value === 'number' ? value : Number(value);
  return Number.isFinite(n) ? n : 0;
}

/** 未处理实体数 = total - redacted（结果页只读展示口径，后端报告同源）。 */
export function unprocessedEntityCount(report: Record<string, unknown> | null): number {
  if (!report) return 0;
  const total = numberFrom(report.total_entities);
  const redacted = numberFrom(report.redacted_entities);
  return Math.max(total - redacted, 0);
}

function distributionRows(
  value: unknown,
  labelForKey: (key: string) => string,
): Array<{ key: string; label: string; count: number }> {
  if (!value || typeof value !== 'object') return [];
  return Object.entries(value as Record<string, unknown>)
    .map(([key, raw]) => ({ key, label: labelForKey(key), count: numberFrom(raw) }))
    .filter((row) => row.count > 0)
    .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label));
}

function sourceLabel(key: string, t: (key: string) => string): string {
  switch (key) {
    case 'regex':
      return t('playground.sourceRegex');
    case 'manual':
      return t('playground.sourceManual');
    case 'has':
      return t('playground.sourceModel');
    case 'llm':
      return t('playground.sourceModel');
    default:
      return key;
  }
}

function confidenceLabel(key: string, t: (key: string) => string): string {
  switch (key) {
    case 'high':
      return t('report.high');
    case 'medium':
      return t('report.medium');
    case 'low':
      return t('report.low');
    default:
      return key;
  }
}

const Metric: FC<{ label: string; value: string }> = ({ label, value }) => (
  <div className="rounded-[14px] border border-border/60 bg-[var(--surface-control)] px-2.5 py-2">
    <p className="text-[11px] text-muted-foreground">{label}</p>
    <p className="mt-0.5 text-sm font-semibold tabular-nums text-foreground">{value}</p>
  </div>
);

const DistributionBlock: FC<{
  title: string;
  rows: Array<{ key: string; label: string; count: number }>;
}> = ({ title, rows }) => {
  if (rows.length === 0) return null;
  return (
    <div>
      <p className="font-medium text-foreground">{title}</p>
      <ul className="mt-1 space-y-0.5">
        {rows.map((row) => (
          <li key={row.key} className="flex items-baseline justify-between gap-3">
            <span className="truncate text-muted-foreground">{row.label}</span>
            <span className="shrink-0 tabular-nums text-foreground">{row.count}</span>
          </li>
        ))}
      </ul>
    </div>
  );
};

/** 质量报告纯展示体（从动作条抽出，供详情弹窗使用；testid 与旧折叠区一致）。 */
export const QualityReportBody: FC<{ report: Record<string, unknown> }> = ({ report }) => {
  const t = useT();
  const typeRows = distributionRows(report.entity_type_distribution, (key) => key);
  const sourceRows = distributionRows(report.source_distribution, (key) => sourceLabel(key, t));
  const confidenceRows = distributionRows(report.confidence_distribution, (key) =>
    confidenceLabel(key, t),
  );
  const redactionMode = typeof report.redaction_mode === 'string' ? report.redaction_mode : null;
  const modeLabel =
    redactionMode === 'structured' ||
    redactionMode === 'smart' ||
    redactionMode === 'mask' ||
    redactionMode === 'pseudonym'
      ? t('playground.processingModeReplace')
      : (redactionMode ?? '—');

  return (
    <div className="space-y-3 text-xs" data-testid="playground-quality-report">
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
        <Metric label={t('playground.totalEntities')} value={String(numberFrom(report.total_entities))} />
        <Metric
          label={t('playground.redactedEntities')}
          value={String(numberFrom(report.redacted_entities))}
        />
        <Metric label={t('playground.unprocessedEntities')} value={String(unprocessedEntityCount(report))} />
        <Metric
          label={t('report.coverage')}
          value={`${numberFrom(report.coverage_rate).toFixed(1)}%`}
        />
      </div>
      <DistributionBlock title={t('report.typeDistribution')} rows={typeRows} />
      <DistributionBlock title={t('playground.sourceDistribution')} rows={sourceRows} />
      <DistributionBlock title={t('report.confidenceDistribution')} rows={confidenceRows} />
      <div className="flex items-baseline justify-between gap-3 rounded-[14px] border border-border/60 bg-[var(--surface-control)] px-2.5 py-2">
        <span className="text-muted-foreground">{t('playground.processingMode')}</span>
        <span className="font-medium text-foreground">{modeLabel}</span>
      </div>
    </div>
  );
};

export interface PlaygroundResultDetailsDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  report: Record<string, unknown> | null;
  processing: PlaygroundProcessingDetailsState;
}

/** 结果页「详情」弹窗：上半质量报告，下半处理详情（总耗时/阶段 rows/失败页清单）。 */
export const PlaygroundResultDetailsDialog: FC<PlaygroundResultDetailsDialogProps> = ({
  open,
  onOpenChange,
  report,
  processing,
}) => {
  const t = useT();
  const hasReport = Boolean(report);
  const hasProcessing = processing.failed || processing.hasData;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className={cn('max-h-[85dvh] w-[min(92vw,42rem)] overflow-y-auto')}
        data-testid="playground-details-dialog"
      >
        <DialogHeader>
          <DialogTitle>{t('playground.detailsDialogTitle')}</DialogTitle>
          <DialogDescription>{t('playground.detailsDialogDesc')}</DialogDescription>
        </DialogHeader>

        {hasReport && (
          <section className="space-y-2" data-testid="playground-details-report-section">
            <h3 className="text-sm font-semibold text-foreground">{t('playground.qualityReport')}</h3>
            <QualityReportBody report={report as Record<string, unknown>} />
          </section>
        )}

        {hasProcessing && (
          <section className="space-y-2" data-testid="playground-details-processing-section">
            <ProcessingDetailsCard
              performance={processing.performance}
              failed={processing.failed}
              errorMessage={processing.errorMessage}
            />
          </section>
        )}

        {!hasReport && !hasProcessing && (
          <p className="text-sm text-muted-foreground" data-testid="playground-details-empty">
            {t('playground.detailsEmpty')}
          </p>
        )}
      </DialogContent>
    </Dialog>
  );
};