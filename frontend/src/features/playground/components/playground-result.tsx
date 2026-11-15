// Copyright 2026 LexMask Contributors

import {
  type CSSProperties,
  type Dispatch,
  type FC,
  type SetStateAction,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { useT } from '@/i18n';
import { RESULT_MARK_HIGHLIGHT_MS } from '@/constants/timing';
import { cn } from '@/lib/utils';
import { getEntityRiskConfig } from '@/config/entityTypes';
import { buildTextSegments } from '@/utils/textRedactionSegments';
import type { VersionHistoryEntry } from '@/types';
import type { BoundingBox, Entity, FileInfo, VisionTypeConfig } from '../types';
import { PlaygroundResultActionBar } from './playground-result-action-bar';
import { TextResultView, ImageResultView, OutputResultView } from './playground-result-views';
import { PlaygroundResultDetailsDialog } from './playground-result-details-dialog';
import { usePlaygroundProcessingDetails } from './processing-details-card';
import { PlaygroundPageExport } from './playground-page-export';
import { useOutputPagePreview } from '../hooks/use-output-page-preview';

export interface PlaygroundResultProps {
  fileInfo: FileInfo | null;
  content: string;
  entities: Entity[];
  entityMap: Record<string, string>;
  redactedCount: number;
  redactionReport: Record<string, unknown> | null;
  reportOpen: boolean;
  setReportOpen: Dispatch<SetStateAction<boolean>>;
  versionHistory: VersionHistoryEntry[];
  versionHistoryOpen: boolean;
  setVersionHistoryOpen: Dispatch<SetStateAction<boolean>>;
  isImageMode: boolean;
  /** Issue#327：当前结果来自子集预览（preview=true 读预览产物，页数为子集页数）。 */
  preview?: boolean;
  previewPageCount?: number | null;
  /** Issue#327 门⑥ C-4：子集 page_map（子集序号 → 原卷页号），page-image 端点按原卷页号取页。 */
  previewPageMap?: Record<string, number> | null;
  imageUrl: string;
  redactedImageUrl?: string;
  redactedImageError?: string | null;
  currentPage: number;
  totalPages: number;
  onPageChange: (page: number) => void;
  visibleBoxes: BoundingBox[];
  /** Issue#322：当前画布全部框（含手动拉框），选页导出时随请求提交。 */
  boundingBoxes: BoundingBox[];
  visionTypes: VisionTypeConfig[];
  getVisionTypeConfig: (typeId: string) => { name: string; color: string };
  onBackToEdit: () => void;
  onReset: () => void;
  onDownload: () => void;
  onDownloadPseudonymCsv?: () => void;
}

export const PlaygroundResult: FC<PlaygroundResultProps> = ({
  fileInfo,
  content,
  entities,
  entityMap,
  redactedCount,
  redactionReport,
  reportOpen,
  setReportOpen,
  versionHistory,
  versionHistoryOpen,
  setVersionHistoryOpen,
  isImageMode,
  preview,
  previewPageCount,
  previewPageMap,
  imageUrl,
  redactedImageUrl,
  redactedImageError,
  currentPage,
  totalPages,
  onPageChange,
  visibleBoxes,
  boundingBoxes,
  visionTypes,
  getVisionTypeConfig,
  onBackToEdit,
  onReset,
  onDownload,
  onDownloadPseudonymCsv,
}) => {
  const t = useT();
  const [mobileTab, setMobileTab] = useState<'original' | 'redacted' | 'mapping'>('original');
  // Issue #83：结果页「对照 | 成品」视图切换（成品=下载文件的实际渲染）
  // Issue#327：子集预览恒为「成品」视图——对照视图是前端按框实时渲染的，
  // 识别在飞时前端没有框（既看不到打码也没有页数语义），只有服务端预览产物可信。
  const [resultView, setResultView] = useState<'compare' | 'output'>(preview ? 'output' : 'compare');
  const [outputPage, setOutputPage] = useState(1);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const clickCounterRef = useRef<Record<string, number>>({});
  const activeMarkTimeoutRef = useRef<number | null>(null);
  const activeMarksRef = useRef<HTMLElement[]>([]);
  const resultReady = !isImageMode || Boolean(redactedImageUrl);

  // 成品视图：文本型 PDF 提供（扫描件/图片的结果页本就实时渲染成品；
  // docx/txt 成品非 PDF 无页图，page-image 端点也不支持）。
  // Issue#327：子集预览必须走服务端产物（扫描件也要）——预览件是后端切页+打码
  // 出来的真 PDF，用 page-image?preview=true 逐页取；前端实时渲染的「成品」在
  // 识别在飞时根本没有框，等于白页。
  const outputPreviewAvailable = preview || (!isImageMode && fileInfo?.file_type === 'pdf');

  // Issue#311：处理详情解析上移到结果页——动作行告警点与「详情」弹窗共用同一份数据；
  // 默认视图不再挂载处理详情卡（DOM 不渲染）。
  const processing = usePlaygroundProcessingDetails(
    fileInfo?.file_id ?? null,
    fileInfo?.page_count ?? fileInfo?.pages?.length ?? null,
  );
  // Issue#311 门⑥ I3：`total - redacted` 含用户主动取消勾选的实体（backend
  // total 含 selected=false），拿它做告警会「复核时一取消勾选就常亮」。告警口径
  // 收紧为「处理失败」：失败页/任务失败；未处理数仍在详情弹窗里如实展示。
  const detailsAlert = processing.failed || processing.failedPages.length > 0;
  const outputPreview = useOutputPagePreview({
    fileId: outputPreviewAvailable ? (fileInfo?.file_id ?? null) : null,
    page: outputPage,
    preview,
    pageCountOverride: previewPageCount ?? undefined,
    pageMap: previewPageMap ?? null,
    enabled: outputPreviewAvailable && resultView === 'output',
  });
  // 翻页时按成品真实页数钳制（docx 回转后成品页数可能与原卷不同；页数未知
  // 时先按原文页数占位钳制，防止首帧前快速点页触发越界 400）
  // 门⑥ 二段 N-3① 复核结论：子集口径首帧不会画成全卷页数——`useOutputPagePreview`
  // 的 url 与 pageCount 同拍写入（同一份响应），而页轨只在 url 有值时才渲染，
  // 故这里沿用既有口径即可，无需为子集单开占位上界。
  const handleOutputPageChange = useCallback(
    (page: number) => {
      const bound = outputPreview.pageCount ?? totalPages;
      setOutputPage(Math.min(Math.max(1, page), Math.max(1, bound)));
    },
    [outputPreview.pageCount, totalPages],
  );

  const isTextPaginated = !isImageMode && totalPages > 1;
  const pageEntities = useMemo(
    () =>
      isTextPaginated
        ? entities.filter((entity) => Number(entity.page || 1) === currentPage)
        : entities,
    [currentPage, entities, isTextPaginated],
  );
  const pages = fileInfo?.pages;
  const displayContent = useMemo(
    () =>
      isTextPaginated && Array.isArray(pages) && pages.length === totalPages
        ? (pages[currentPage - 1] ?? content)
        : content,
    [content, currentPage, isTextPaginated, pages, totalPages],
  );
  const displayEntityMap = useMemo(() => {
    if (!isTextPaginated) return entityMap;
    const selectedTexts = new Set(
      pageEntities.filter((entity) => entity.selected !== false).map((entity) => entity.text),
    );
    return Object.fromEntries(Object.entries(entityMap).filter(([key]) => selectedTexts.has(key)));
  }, [entityMap, isTextPaginated, pageEntities]);

  const origToTypeId = useMemo(() => {
    const out = new Map<string, string>();
    const fullContent = content || '';
    for (const entity of pageEntities) {
      if (!entity.selected || displayEntityMap[entity.text] === undefined) continue;
      out.set(entity.text, String(entity.type));
      if (
        typeof entity.start === 'number' &&
        typeof entity.end === 'number' &&
        entity.end <= fullContent.length
      ) {
        const slice = fullContent.slice(entity.start, entity.end);
        if (slice && slice !== entity.text) out.set(slice, String(entity.type));
      }
    }
    return out;
  }, [content, displayEntityMap, pageEntities]);

  const segments = useMemo(
    () => buildTextSegments(displayContent, displayEntityMap),
    [displayContent, displayEntityMap],
  );
  const matchCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const segment of segments) {
      if (segment.isMatch) {
        counts.set(segment.origKey, (counts.get(segment.origKey) || 0) + 1);
      }
    }
    return counts;
  }, [segments]);

  const markStyleForOrig = useCallback(
    (origKey: string): CSSProperties => {
      const typeId = origToTypeId.get(origKey) ?? '';
      const config = getEntityRiskConfig(typeId || 'CUSTOM');
      return {
        backgroundColor: config.bgColor,
        color: config.textColor,
        boxShadow: `inset 0 -2px 0 ${config.color}55`,
      };
    },
    [origToTypeId],
  );

  const clearActiveMarks = useCallback(() => {
    if (activeMarkTimeoutRef.current !== null) {
      window.clearTimeout(activeMarkTimeoutRef.current);
      activeMarkTimeoutRef.current = null;
    }
    for (const element of activeMarksRef.current) {
      element.classList.remove(
        'result-mark-active',
        'ring-2',
        'ring-offset-1',
        'ring-blue-400/80',
        'scale-105',
      );
    }
    activeMarksRef.current = [];
  }, []);

  useEffect(() => () => clearActiveMarks(), [clearActiveMarks]);

  const scrollToMatch = useCallback(
    (original: string) => {
      const safeKey = original.replace(/[^a-zA-Z0-9\u4e00-\u9fff]/g, '_');
      const root = rootRef.current;
      if (!root) return;
      const originalMarks = root.querySelectorAll(`.result-mark-orig[data-match-key="${safeKey}"]`);
      const redactedMarks = root.querySelectorAll(
        `.result-mark-redacted[data-match-key="${safeKey}"]`,
      );
      const total = Math.max(originalMarks.length, redactedMarks.length);
      if (total === 0) return;

      const index = (clickCounterRef.current[safeKey] || 0) % total;
      clickCounterRef.current[safeKey] = index + 1;

      clearActiveMarks();

      const originalElement = originalMarks[Math.min(index, originalMarks.length - 1)] as
        | HTMLElement
        | undefined;
      originalElement?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      originalElement?.classList.add(
        'result-mark-active',
        'ring-2',
        'ring-offset-1',
        'ring-blue-400/80',
        'scale-105',
      );

      const redactedElement = redactedMarks[Math.min(index, redactedMarks.length - 1)] as
        | HTMLElement
        | undefined;
      redactedElement?.scrollIntoView({ behavior: 'smooth', block: 'center' });
      redactedElement?.classList.add(
        'result-mark-active',
        'ring-2',
        'ring-offset-1',
        'ring-blue-400/80',
        'scale-105',
      );

      activeMarksRef.current = [originalElement, redactedElement].filter(Boolean) as HTMLElement[];
      activeMarkTimeoutRef.current = window.setTimeout(() => {
        clearActiveMarks();
      }, RESULT_MARK_HIGHLIGHT_MS);
    },
    [clearActiveMarks],
  );

  const renderOriginal = useCallback(
    () =>
      segments.map((segment, index) =>
        segment.isMatch ? (
          <mark
            key={index}
            data-match-key={segment.safeKey}
            data-match-idx={segment.matchIdx}
            style={markStyleForOrig(segment.origKey)}
            className="result-mark-orig rounded-md px-0.5 transition-all duration-300"
          >
            {segment.text}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      ),
    [markStyleForOrig, segments],
  );

  const renderRedacted = useCallback(
    () =>
      segments.map((segment, index) =>
        segment.isMatch ? (
          <mark
            key={index}
            data-match-key={segment.safeKey}
            data-match-idx={segment.matchIdx}
            style={markStyleForOrig(segment.origKey)}
            className="result-mark-redacted rounded-md px-0.5 transition-all duration-300"
          >
            {displayEntityMap[segment.origKey]}
          </mark>
        ) : (
          <span key={index}>{segment.text}</span>
        ),
      ),
    [displayEntityMap, markStyleForOrig, segments],
  );

  return (
    <div
      ref={rootRef}
      className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
      data-testid="playground-result"
    >
      <PlaygroundResultActionBar
        fileInfo={fileInfo}
        canDownload={resultReady}
        detailsAlert={detailsAlert}
        onOpenDetails={() => setReportOpen(true)}
        onBackToEdit={onBackToEdit}
        onReset={onReset}
        onDownload={onDownload}
        onDownloadPseudonymCsv={onDownloadPseudonymCsv}
      />

      {/* Issue#311：质量报告 + 处理详情合并进「详情」弹窗；默认关闭，Radix 负责 ESC/遮罩/焦点。 */}
      <PlaygroundResultDetailsDialog
        open={reportOpen}
        onOpenChange={setReportOpen}
        report={redactionReport}
        processing={processing}
      />

      {/* Issue#257 需求重对齐：单文件任务也要能看逐页结果、选页导出（运行中/结果页同一块）。
          Issue#311：默认收起（summary 仍可见），与「对照 | 成品」同一行，把高度让给预览。 */}
      <div
        className="mb-2 flex shrink-0 flex-wrap items-center gap-2"
        data-testid="playground-result-toolbar"
      >
        <div className="min-w-0 flex-1">
          <PlaygroundPageExport
            fileId={fileInfo?.file_id ?? null}
            defaultCollapsed
            onRequestClose={onBackToEdit}
            boundingBoxes={boundingBoxes}
          />

        </div>
        {preview && (
          // Issue#327 验收口径②：结果页必须让用户看见「本次只含这批页、其余页不在
          // 本次范围」——仅一次性 toast 会被忽略，页轨又看着是全卷页数。
          <span
            className="flex shrink-0 items-center self-center whitespace-nowrap rounded-xl border border-amber-500/40 bg-amber-500/10 px-2.5 py-1 text-xs font-medium text-amber-700 dark:text-amber-400"
            data-testid="playground-result-subset-banner"
          >
            {t('playground.subsetBanner')
              .replace('{done}', String(previewPageCount ?? 0))
              .replace('{total}', String(totalPages))}
          </span>
        )}
        {outputPreviewAvailable && !preview && (
          <div
            className="flex shrink-0 items-center gap-1 self-center rounded-2xl border border-border/60 bg-[var(--surface-control)] p-1 shadow-[var(--shadow-control)]"
            data-testid="playground-result-view-toggle"
          >
            {(
              [
                ['compare', t('playground.resultViewToggle.compare')],
                ['output', t('playground.resultViewToggle.output')],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                onClick={() => setResultView(key)}
                className={cn(
                  'whitespace-nowrap rounded-xl px-3 py-1.5 text-xs font-medium transition-colors',
                  resultView === key
                    ? 'bg-card text-foreground shadow-sm'
                    : 'text-muted-foreground hover:text-foreground',
                )}
                data-testid={`playground-result-view-${key}`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      {resultView === 'compare' && (
        <div className="mx-3 flex shrink-0 gap-1 rounded-t-[20px] border border-border/60 border-b-0 bg-background px-2 pt-2 md:hidden">
          {(
            [
              ['original', t('playground.mobile.original')],
              ['redacted', t('playground.mobile.redacted')],
              ['mapping', t('playground.mobile.mapping')],
            ] as const
          ).map(([key, label]) => (
            <button
              key={key}
              onClick={() => setMobileTab(key)}
              className={cn(
                'rounded-xl px-3 py-2 text-xs font-medium transition-colors',
                mobileTab === key ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground',
              )}
            >
              {label}
            </button>
          ))}
        </div>
      )}

      <div
        className="flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
        data-testid="playground-result-preview"
      >
        {resultView === 'output' && outputPreviewAvailable ? (
        <OutputResultView
          url={outputPreview.url}
          pageCount={outputPreview.pageCount}
          fallbackPageCount={totalPages}
          loading={outputPreview.loading}
          error={outputPreview.error}
          currentPage={outputPage}
          onPageChange={handleOutputPageChange}
          getVisionTypeConfig={getVisionTypeConfig}
        />
      ) : isImageMode ? (
        <ImageResultView
          fileInfo={fileInfo}
          imageUrl={imageUrl}
          redactedImageUrl={redactedImageUrl}
          redactedImageError={redactedImageError}
          currentPage={currentPage}
          totalPages={totalPages}
          onPageChange={onPageChange}
          visibleBoxes={visibleBoxes}
          visionTypes={visionTypes}
          getVisionTypeConfig={getVisionTypeConfig}
          entityMap={entityMap}
          redactedCount={redactedCount}
          origToTypeId={origToTypeId}
          matchCounts={matchCounts}
          scrollToMatch={scrollToMatch}
          mobileTab={mobileTab}
          versionHistory={versionHistory}
          versionHistoryOpen={versionHistoryOpen}
          setVersionHistoryOpen={setVersionHistoryOpen}
        />
      ) : (
        <TextResultView
          renderOriginal={renderOriginal}
          renderRedacted={renderRedacted}
          content={displayContent}
          entityMap={displayEntityMap}
          redactedCount={redactedCount}
          origToTypeId={origToTypeId}
          matchCounts={matchCounts}
          scrollToMatch={scrollToMatch}
          mobileTab={mobileTab}
          versionHistory={versionHistory}
          versionHistoryOpen={versionHistoryOpen}
          setVersionHistoryOpen={setVersionHistoryOpen}
          currentPage={currentPage}
          totalPages={totalPages}
          onPageChange={onPageChange}
        />
      )}
      </div>
    </div>
  );
};
