// Copyright 2026 LexMask Contributors

import { type FC, memo, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, XCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { useT } from '@/i18n';
import { PLAYGROUND_LOADING_TICK_MS, PLAYGROUND_LONG_RUNNING_HINT_MS } from '@/constants/timing';
import { useInteractionLock } from '@/hooks/use-interaction-lock';
import {
  buildProcessingRows,
  type ProcessingRow,
  type ProcessingRowKey,
} from '../lib/processing-details';

export interface PlaygroundLoadingProps {
  loadingMessage: string;
  isImageMode: boolean;
  onCancel?: () => void;
  /**
   * Issue#123：遮罩形态。
   * - `blocking`（默认）：既有行为不变——全屏半透明 + backdrop-blur + 交互锁，整页遮挡。
   * - `passive`：透明、不模糊、右下角小面板、不锁交互——让逐页回流的框在识别期间可见
   *   （画布此时由 `ImageBBoxEditor` 的 `lockDraw` 置只读，见 playground-page.tsx）。
   */
  overlay?: 'blocking' | 'passive';
  /**
   * 真实进度（Issue#123 页流式 / Issue#194 任务轮询，均为 completed/total）。
   * total > 0 时进度条用真值；缺省回落到既有的时间 fake 公式。
   */
  progress?: { completed: number; total: number } | null;
  /**
   * Issue#194 用户验收④：等待页不再渲染阶段耗时明细（秒数只留在结果页详情卡）。
   * performance 现在只喂「✓ 已完成清单」——buildProcessingRows 的行里非当前段的行，
   * 按固定顺序渲染成不跳动的静态清单。
   */
  performance?: Record<string, unknown> | null;
  /**
   * Issue#194 用户验收③④：当前段人话段名（hook 由任务 progress_message 经
   * recognitionStageFromMessage 映射）。null/缺省 = 无段名语境（解析段/旧流式）——
   * 不渲染段名行，清单不排除任何行。
   */
  stage?: string | null;
}

/** ✓ 清单固定顺序（用户验收④ mockup）；排队/打码/失败回落等行不上等待页。 */
const CHECKLIST_ORDER: ProcessingRowKey[] = ['parse', 'ocr', 'ner', 'visual_features'];

/**
 * 当前段人话段名 → 行键（两侧同走一个 t，locale 安全）；无对应行的段名
 * （泛「视觉识别」段、未知段）返回 null——清单不排除任何行。
 */
function currentStageRowKey(
  stage: string | null | undefined,
  t: (key: string) => string,
): ProcessingRowKey | null {
  if (!stage) return null;
  if (stage === t('playground.taskStage.visualFeatures')) return 'visual_features';
  if (stage === t('playground.taskStage.ocrHas')) return 'ocr';
  if (stage === t('playground.taskStage.text')) return 'ner';
  return null;
}

export const PlaygroundLoading: FC<PlaygroundLoadingProps> = memo(
  ({ loadingMessage, isImageMode, onCancel, overlay = 'blocking', progress = null, performance = null, stage = null }) => {
    const t = useT();
    const panelRef = useRef<HTMLDivElement>(null);
    const [elapsedMs, setElapsedMs] = useState(0);
    const elapsedSec = Math.floor(elapsedMs / 1_000);
    const safeElapsedSec = Math.max(0, elapsedSec);
    // Issue#194：有真实 current/total 时进度条用真值；假公式仅作 fallback。
    const progressTotal = progress?.total ?? 0;
    const progressValue =
      progressTotal > 0
        ? Math.min(100, Math.round(((progress?.completed ?? 0) / progressTotal) * 100))
        : Math.min(92, Math.round(12 + 80 * (1 - Math.exp(-safeElapsedSec / 90))));
    const showLongRunningHint = elapsedMs >= PLAYGROUND_LONG_RUNNING_HINT_MS;
    // Issue#123：passive 不锁交互——遮罩透明且 pointer-events:none，用户可滚动/缩放画布
    // （画布编辑由 lockDraw 单独置只读，避免识别中途改框）。
    useInteractionLock(overlay === 'blocking', panelRef);

    useEffect(() => {
      const startedAt = Date.now();
      const timer = window.setInterval(() => {
        setElapsedMs(Math.max(0, Date.now() - startedAt));
      }, PLAYGROUND_LOADING_TICK_MS);
      return () => window.clearInterval(timer);
    }, []);

    // 用户验收④：当前段行（段名 + 页码）是等待页唯一的活数字；已完成段进静态清单，
    // 不再同时跳秒数计时 / 阶段耗时明细 / 两段式回零数字多处变化。
    const stageLine =
      stage && progressTotal > 0 ? `${stage} ${progress?.completed ?? 0}/${progressTotal}` : null;
    const currentRowKey = currentStageRowKey(stage, t);
    const rows = buildProcessingRows(performance);
    const checklist = CHECKLIST_ORDER.map((key) => rows.find((row) => row.key === key)).filter(
      (row): row is ProcessingRow => row !== undefined && row.key !== currentRowKey,
    );

    return createPortal(
      <div
        className={
          overlay === 'passive'
            ? 'pointer-events-none fixed inset-0 z-50 flex items-end justify-end p-3'
            : 'fixed inset-0 z-50 flex cursor-wait items-center justify-center bg-background/70 px-4 backdrop-blur-[2px]'
        }
        style={{ pointerEvents: overlay === 'passive' ? 'none' : 'auto' }}
        role="status"
        aria-busy="true"
        aria-label={loadingMessage || t('playground.processing')}
        data-testid="playground-loading"
        data-overlay={overlay}
      >
        <div
          ref={panelRef}
          tabIndex={-1}
          className={
            overlay === 'passive'
              ? 'pointer-events-auto w-full max-w-xs animate-scale-in cursor-default rounded-[20px] border border-border/70 bg-[var(--surface-overlay)] px-4 py-3.5 text-left shadow-[var(--shadow-floating)] outline-none'
              : 'w-full max-w-md animate-scale-in cursor-default rounded-[20px] border border-border/70 bg-[var(--surface-overlay)] px-4 py-3.5 text-left shadow-[var(--shadow-floating)] outline-none'
          }
        >
          <div className="mb-3 flex items-start gap-3">
            <div className="mt-0.5 size-5 shrink-0 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-foreground">
                {loadingMessage || t('playground.processing')}
              </p>
              {stageLine && (
                <p
                  className="mt-1 text-xs tabular-nums text-muted-foreground"
                  data-testid="playground-loading-stage"
                >
                  {stageLine}
                </p>
              )}
            </div>
          </div>
          <Progress
            value={progressValue}
            className="mb-3"
            aria-label={t('playground.loadingProgress')}
            data-testid="playground-loading-progress"
          />

          {/* 用户验收④：✓ 已完成清单——固定顺序、非当前段、不跳动 */}
          {checklist.length > 0 && (
            <div className="mb-3 space-y-1" data-testid="playground-loading-checklist">
              {checklist.map((row) => (
                <div
                  key={row.key}
                  data-testid={`playground-checklist-${row.key}`}
                  className="flex items-baseline justify-between gap-3 text-xs text-muted-foreground"
                >
                  <span className="flex shrink-0 items-center gap-1">
                    <Check
                      aria-hidden="true"
                      className="size-3.5 text-emerald-600 dark:text-emerald-400"
                    />
                    {t(row.labelKey)}
                  </span>
                  {row.counts !== null && (
                    <span className="truncate text-right tabular-nums">{row.counts}</span>
                  )}
                </div>
              ))}
            </div>
          )}

          {isImageMode ? (
            <>
              <p className="text-xs leading-relaxed text-muted-foreground">
                {t('playground.imageHint')}{' '}
                <strong className="font-medium text-foreground">
                  {t('playground.cpuWarning')}
                </strong>{' '}
                {t('playground.waitHint')}
              </p>
              {showLongRunningHint && (
                <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                  {t('playground.longRunningHint')}
                </p>
              )}
            </>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">{t('playground.processingHint')}</p>
              {showLongRunningHint && (
                <p className="mt-2 text-xs leading-relaxed text-muted-foreground">
                  {t('playground.longRunningHint')}
                </p>
              )}
            </>
          )}

          {onCancel && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="mt-4 w-full whitespace-nowrap"
              onClick={onCancel}
              data-testid="playground-cancel-processing"
            >
              <XCircle aria-hidden="true" />
              {t('common.cancel')}
            </Button>
          )}
        </div>
      </div>,
      document.body,
    );
  },
);
