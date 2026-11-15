// Copyright 2026 LexMask Contributors

// Issue#311：结果页默认视图瘦身——旧「匿名化完成」深色横幅整块删除，
// 只保留一行右对齐动作按钮（返回编辑 / 处理新文件 / 化名对照表 / 下载），
// 水印 chip 降级为按钮行内小徽标（合规证据仍可见），质量报告与处理详情
// 收进「详情」弹窗（见 playground-result-details-dialog.tsx）。

import { useEffect, useState, type FC } from 'react';
import { AlertTriangle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { useT } from '@/i18n';
import { authFetch } from '@/services/api-client';
import type { FileInfo } from '../types';
import {
  watermarkOverlayStatusFromVisionQuality,
  watermarkStripChipText,
} from '@/lib/watermark-overlay';

export interface PlaygroundResultActionBarProps {
  fileInfo: FileInfo | null;
  canDownload?: boolean;
  /** 有失败页/未处理项/任务失败时为 true——详情入口带琥珀色告警点，信息不可被隐藏。 */
  detailsAlert?: boolean;
  onOpenDetails: () => void;
  onBackToEdit: () => void;
  onReset: () => void;
  onDownload: () => void | Promise<void>;
  /** 替换（化名）模式执行成功后提供，下载化名对照表 csv */
  onDownloadPseudonymCsv?: () => void;
}

export const PlaygroundResultActionBar: FC<PlaygroundResultActionBarProps> = ({
  fileInfo,
  canDownload = true,
  detailsAlert = false,
  onOpenDetails,
  onBackToEdit,
  onReset,
  onDownload,
  onDownloadPseudonymCsv,
}) => {
  const t = useT();
  // Issue#236：水印预处理命中时显式报「已摘除 N 页」——`region_count` 恒 0，
  // 只靠通用状态行用户看不出这一步到底做了什么。
  // 草稿快照恢复回来的会话（老 draft）可能没带 vision_quality：自取一次文件详情，
  // 否则用户点「继续审阅」回场就看不到这行证据。
  const [fetchedVisionQuality, setFetchedVisionQuality] = useState<unknown>(undefined);
  const fileId = fileInfo?.file_id;
  const hasInlineQuality = fileInfo?.vision_quality !== undefined;
  useEffect(() => {
    if (!fileId || hasInlineQuality) return;
    let cancelled = false;
    void (async () => {
      try {
        const res = await authFetch(`/api/v1/files/${fileId}`);
        if (!res.ok || cancelled) return;
        const info = (await res.json()) as Record<string, unknown>;
        if (!cancelled) setFetchedVisionQuality(info.vision_quality);
      } catch {
        // 取不到就不显示 chip：宁可少一行，也不谎报「已摘除」
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [fileId, hasInlineQuality]);

  const watermarkChip = watermarkStripChipText(
    t,
    watermarkOverlayStatusFromVisionQuality(
      hasInlineQuality ? fileInfo?.vision_quality : fetchedVisionQuality,
    ),
  );
  const [downloading, setDownloading] = useState(false);

  const handleDownloadClick = () => {
    if (downloading) return;
    setDownloading(true);
    void Promise.resolve(onDownload()).finally(() => setDownloading(false));
  };

  return (
    <div
      className="mb-2 flex shrink-0 flex-wrap items-center justify-end gap-2"
      data-testid="playground-result-action-bar"
    >
      {watermarkChip ? (
        <span
          className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap rounded-full border border-border/70 bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
          data-testid="playground-watermark-chip"
        >
          {t('batchWizard.step4.pipelineWatermarkOverlay')} {watermarkChip}
        </span>
      ) : null}

      <Button
        variant="outline"
        size="sm"
        className="relative h-9 px-3"
        onClick={onOpenDetails}
        data-testid="playground-details-button"
        aria-label={
          detailsAlert ? `${t('playground.details')} · ${t('playground.detailsAlert')}` : t('playground.details')
        }
      >
        {detailsAlert ? (
          <>
            <AlertTriangle aria-hidden="true" className="mr-1 size-3.5 text-amber-600 dark:text-amber-400" />
            {t('playground.details')}
            <span
              className="absolute -right-1 -top-1 size-2.5 rounded-full bg-amber-500 ring-2 ring-background"
              data-testid="playground-details-alert"
              aria-hidden="true"
            />
          </>
        ) : (
          t('playground.details')
        )}
      </Button>
      <Button
        variant="secondary"
        size="sm"
        className="h-9 px-3"
        onClick={onBackToEdit}
        data-testid="playground-back-edit"
      >
        {t('playground.backToEdit')}
      </Button>
      <Button variant="outline" size="sm" className="h-9 px-3" onClick={onReset} data-testid="playground-new-file">
        {t('playground.newFile')}
      </Button>
      {onDownloadPseudonymCsv && (
        <Button
          variant="outline"
          size="sm"
          className="h-9 px-3"
          onClick={onDownloadPseudonymCsv}
          data-testid="playground-download-pseudonym-csv"
        >
          {t('playground.downloadPseudonymCsv')}
        </Button>
      )}
      {fileInfo && canDownload && (
        <Button
          size="sm"
          className="h-9 px-3"
          onClick={handleDownloadClick}
          disabled={downloading}
          data-testid="playground-download"
        >
          {downloading ? t('common.downloading') : t('playground.downloadFile')}
        </Button>
      )}
    </div>
  );
};
