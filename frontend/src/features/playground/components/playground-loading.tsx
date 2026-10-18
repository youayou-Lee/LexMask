// Copyright 2026 LexMask Contributors

import { type FC, memo, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { XCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { useT } from '@/i18n';
import { PLAYGROUND_LOADING_TICK_MS, PLAYGROUND_LONG_RUNNING_HINT_MS } from '@/constants/timing';
import { useInteractionLock } from '@/hooks/use-interaction-lock';

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
}

export const PlaygroundLoading: FC<PlaygroundLoadingProps> = memo(
  ({ loadingMessage, isImageMode, onCancel, overlay = 'blocking', progress = null }) => {
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
              <p
                className="mt-1 text-xs tabular-nums text-muted-foreground"
                data-testid="playground-loading-timer"
              >
                {t('playground.waited')} {safeElapsedSec} {t('playground.seconds')}...
              </p>
            </div>
          </div>
          <Progress
            value={progressValue}
            className="mb-3"
            aria-label={t('playground.loadingProgress')}
            data-testid="playground-loading-progress"
          />

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
