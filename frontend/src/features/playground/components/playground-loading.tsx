// Copyright 2026 LexMask Contributors

import { type FC, type ReactNode, memo, useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { Check, XCircle } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Progress } from '@/components/ui/progress';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { PLAYGROUND_LOADING_TICK_MS, PLAYGROUND_LONG_RUNNING_HINT_MS } from '@/constants/timing';
import { useInteractionLock } from '@/hooks/use-interaction-lock';
import {
  buildProcessingRows,
  formatDurationMs,
  type ProcessingRow,
  type ProcessingRowKey,
} from '../lib/processing-details';
import { STAGE_HINT_KEYS, STAGE_LABEL_KEYS, type StagesView } from '../lib/task-stages';

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
   * Issue#238：completed/total 语义 = **页完成数**（后端每页终态上报），不再是开跑页号。
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
  /**
   * Issue#238 段信息（performance.recognition.current_segment）。
   * total > 1 时段名行显示「第 X 段/共 Y 段」；单段（缺键）不编号。
   */
  segment?: { index: number; total: number } | null;
  /** Issue#238 并行处理中页数 = min(该段页并发, 剩余页)；null/<=0 不显示。 */
  inFlight?: number | null;
  /** Issue#238 任务段 started_at（ISO）——已运行时长以后端起算，刷新不走表；缺省回落本地计时。 */
  startedAt?: string | null;
  /** Issue#238 文本件：无页级计数——数字行显示状态词、进度条不定态。 */
  textMode?: boolean;
  /** Issue#238 redaction = 打码段被动小卡（右下角，无清单/提示/取消，可离开本页语义）。 */
  variant?: 'recognition' | 'redaction';
  /**
   * Issue#257 需求重对齐：识别中就要能看逐页结果、选页导出——投在收尾的
   * 槽位里（页格自带宽高限制，遮罩面板不会被撑爆）。
   */
  children?: ReactNode;
  /**
   * Issue#274 三段式阶段条视图（stages 账派生，或乐观①）。
   * null/缺省 = 无阶段语义语境（parse 窗口/旧任务/文本件）——不渲染阶段行，
   * 既有渲染逐字节不变（AC6 零回归面）。
   */
  stagesView?: StagesView | null;
}

/** ✓ 清单固定顺序（用户验收④ mockup）；排队/打码/失败回落等行不上等待页。 */
const CHECKLIST_ORDER: ProcessingRowKey[] = ['parse', 'ocr', 'ner', 'visual_features'];

/**
 * 当前段**进行中**的行集合（AC2：进行中的活不打 ✓）。
 * - OCR+HaS 段：OCR 与 NER 两行都在跑（NER 逐页内嵌在 ocr_has 管线里）；
 * - 视觉要素段：只视觉行在跑；
 * - 泛「视觉识别」段（单段扫/单图）：OCR/NER/视觉一次合并跑完，三行都算进行中；
 * - 文本件 NER 段：NER 行在跑；
 * 无对应行的段名（未知段）返回空集——清单不排除任何行。
 */
function currentStageRowKeys(
  stage: string | null | undefined,
  t: (key: string) => string,
): Set<ProcessingRowKey> {
  if (!stage) return new Set();
  if (stage === t('playground.taskStage.visualFeatures')) return new Set(['visual_features']);
  if (stage === t('playground.taskStage.ocrHas')) return new Set(['ocr', 'ner']);
  if (stage === t('playground.taskStage.vision')) return new Set(['ocr', 'ner', 'visual_features']);
  if (stage === t('playground.taskStage.text')) return new Set(['ner']);
  return new Set();
}

/** i18n 模板 {param} 填充（processing-details 同款写法，组件本地小副本）。 */
function fillTemplate(template: string, params: Record<string, string>): string {
  return Object.entries(params).reduce(
    (acc, [name, value]) => acc.split(`{${name}}`).join(value),
    template,
  );
}

export const PlaygroundLoading: FC<PlaygroundLoadingProps> = memo(
  ({
    loadingMessage,
    isImageMode,
    onCancel,
    overlay = 'blocking',
    progress = null,
    performance = null,
    stage = null,
    segment = null,
    inFlight = null,
    startedAt = null,
    textMode = false,
    variant = 'recognition',
    stagesView = null,
    children,
  }) => {
    const t = useT();
    const panelRef = useRef<HTMLDivElement>(null);
    const [localElapsedMs, setLocalElapsedMs] = useState(0);
    const [nowMs, setNowMs] = useState(() => Date.now());
    // Issue#238：已运行优先以后端任务 started_at 起算（刷新不走表）；缺键回落本地计时。
    const parsedStartedMs = startedAt ? Date.parse(startedAt) : NaN;
    const elapsedMs = Number.isFinite(parsedStartedMs)
      ? Math.max(0, nowMs - parsedStartedMs)
      : localElapsedMs;
    const elapsedSec = Math.floor(elapsedMs / 1_000);
    const safeElapsedSec = Math.max(0, elapsedSec);
    // Issue#194：有真实 current/total 时进度条用真值；文本件不定态；假公式仅作 vision 无计数段 fallback。
    const progressTotal = progress?.total ?? 0;
    const barValue = textMode
      ? null
      : progressTotal > 0
        ? Math.min(100, Math.round(((progress?.completed ?? 0) / progressTotal) * 100))
        : Math.min(92, Math.round(12 + 80 * (1 - Math.exp(-safeElapsedSec / 90))));
    const showLongRunningHint = elapsedMs >= PLAYGROUND_LONG_RUNNING_HINT_MS;
    // Issue#123：passive 不锁交互——遮罩透明且 pointer-events:none，用户可滚动/缩放画布
    // （画布编辑由 lockDraw 单独置只读，避免识别中途改框）。
    useInteractionLock(variant === 'recognition' && overlay === 'blocking', panelRef);

    useEffect(() => {
      const startedAtLocal = Date.now();
      const timer = window.setInterval(() => {
        const now = Date.now();
        setLocalElapsedMs(Math.max(0, now - startedAtLocal));
        setNowMs(now);
      }, PLAYGROUND_LOADING_TICK_MS);
      return () => window.clearInterval(timer);
    }, []);

    const elapsedLabel = fillTemplate(t('playground.taskProgress.elapsed'), {
      time: formatDurationMs(elapsedMs),
    });

    if (variant === 'redaction') {
      // Issue#238 打码段被动小卡：逐页数字（无页回调时回落不定态文案）+ 已运行，
      // 不遮挡预览页（「点完打码即可离开」语义不变）。
      const redTotal = progress?.total ?? 0;
      // 打码卡不套识别段的假公式：有逐页数据用真值，无页级回调（文本/docx 快链路）走不定态
      const redBarValue =
        redTotal > 0 ? Math.min(100, Math.round(((progress?.completed ?? 0) / redTotal) * 100)) : null;
      const redLine =
        redTotal > 0
          ? fillTemplate(t('playground.redactProgressLine'), {
              completed: String(progress?.completed ?? 0),
              total: String(redTotal),
              time: formatDurationMs(elapsedMs),
            })
          : fillTemplate(t('playground.redactProgressIndeterminate'), {
              time: formatDurationMs(elapsedMs),
            });
      return createPortal(
        <div
          className="pointer-events-none fixed inset-0 z-50 flex items-end justify-end p-3"
          role="status"
          aria-busy="true"
          aria-label={loadingMessage || t('playground.taskRedacting')}
          data-testid="playground-loading"
          data-overlay={overlay}
          data-variant="redaction"
        >
          <div
            ref={panelRef}
            tabIndex={-1}
            className="pointer-events-auto w-full max-w-xs animate-scale-in cursor-default rounded-[20px] border border-border/70 bg-[var(--surface-overlay)] px-4 py-3.5 text-left shadow-[var(--shadow-floating)] outline-none"
          >
            <div className="mb-2 flex items-start gap-2.5">
              <div className="mt-0.5 size-4 shrink-0 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm font-medium text-foreground">
                  {loadingMessage || t('playground.taskRedacting')}
                </p>
                <p
                  className="mt-1 text-xs tabular-nums text-muted-foreground"
                  data-testid="playground-redact-line"
                >
                  {redLine}
                </p>
              </div>
            </div>
            {redBarValue === null ? (
              <div
                className="h-2 w-full overflow-hidden rounded-full bg-primary/20"
                data-testid="playground-loading-progress"
                data-indeterminate="true"
              >
                <div className="h-full w-1/3 animate-indeterminate-slide rounded-full bg-primary" />
              </div>
            ) : (
              <Progress
                value={redBarValue}
                aria-label={t('playground.loadingProgress')}
                data-testid="playground-loading-progress"
              />
            )}
          </div>
        </div>,
        document.body,
      );
    }

    // 数字行（AC1/AC3）：真完成数 + 并行处理中；文本件无页级计数 → 状态词 + 不定态条。
    const numsLine = textMode
      ? t('playground.taskProgress.textRunning')
      : progressTotal > 0
        ? fillTemplate(t('playground.taskProgress.completed'), {
            completed: String(progress?.completed ?? 0),
            total: String(progressTotal),
          }) +
          (inFlight !== null && inFlight > 0
            ? ` · ${fillTemplate(t('playground.taskProgress.inFlight'), { n: String(inFlight) })}`
            : '')
        : null;

    // Issue#274：阶段行（stagesView 非 null 才渲染；null ⇒ 不渲染、既有布局不变）。
    // 形态 = `✓ 已完成段 → 当前段 · 一句话说明 · 约 Xs`；加速器并行段附在后段。
    const stageBarRows: Array<{ key: string; label: string; hint: string | null; done: boolean }> = [];
    if (stagesView) {
      for (const name of stagesView.done) {
        const labelKey = STAGE_LABEL_KEYS[name];
        if (labelKey) {
          stageBarRows.push({ key: name, label: t(labelKey), hint: null, done: true });
        }
      }
      if (stagesView.current) {
        const labelKey = STAGE_LABEL_KEYS[stagesView.current];
        if (labelKey) {
          const hintKey = STAGE_HINT_KEYS[stagesView.current];
          stageBarRows.push({
            key: stagesView.current,
            label: t(labelKey),
            hint: hintKey ? t(hintKey) : null,
            done: false,
          });
        }
      }
    }
    const etaSuffix = stagesView
      ? stagesView.etaSec !== null
        ? fillTemplate(t('playground.taskProgress.etaAbout'), {
            time: formatDurationMs(stagesView.etaSec * 1000),
          })
        : stagesView.perPageSec !== null
          ? fillTemplate(t('playground.taskProgress.perPage'), {
              sec: String(stagesView.perPageSec),
            })
          : null
      : null;

    // 段名行（AC3）：两段才编号；单段/缺段信息只显示段名。
    const stageLabel = stage ?? null;
    const segmentLabel =
      stageLabel && segment && segment.total > 1
        ? fillTemplate(t('playground.taskProgress.segment'), {
            index: String(segment.index),
            total: String(segment.total),
            stage: stageLabel,
          })
        : stageLabel;

    // 用户验收④：当前段行（集合）从清单排除（AC2：进行中的活不打 ✓）；已完成段进静态清单。
    const currentKeys = currentStageRowKeys(stage, t);
    const rows = buildProcessingRows(performance);
    const checklist = CHECKLIST_ORDER.map((key) => rows.find((row) => row.key === key)).filter(
      (row): row is ProcessingRow => row !== undefined && !currentKeys.has(row.key),
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
        data-variant="recognition"
      >
        <div
          ref={panelRef}
          tabIndex={-1}
          className={
            overlay === 'passive'
              ? 'pointer-events-auto w-full max-w-xs animate-scale-in cursor-default rounded-[20px] border border-border/70 bg-[var(--surface-overlay)] px-4 py-3.5 text-left shadow-[var(--shadow-floating)] outline-none'
              : `w-full ${children ? 'max-w-xl' : 'max-w-md'} animate-scale-in cursor-default rounded-[20px] border border-border/70 bg-[var(--surface-overlay)] px-4 py-3.5 text-left shadow-[var(--shadow-floating)] outline-none`
          }
        >
          <div className="mb-3 flex items-start gap-3">
            <div className="mt-0.5 size-5 shrink-0 animate-spin rounded-full border-2 border-primary/30 border-t-primary" />
            <div className="min-w-0 flex-1">
              <p className="truncate text-sm font-medium text-foreground">
                {loadingMessage || t('playground.processing')}
              </p>
              {numsLine && (
                <p
                  className="mt-1 text-xs tabular-nums text-muted-foreground"
                  data-testid="playground-loading-nums"
                >
                  {numsLine}
                </p>
              )}
            </div>
          </div>
          {barValue === null ? (
            <div
              className="mb-3 h-2 w-full overflow-hidden rounded-full bg-primary/20"
              data-testid="playground-loading-progress"
              data-indeterminate="true"
            >
              <div className="h-full w-1/3 animate-indeterminate-slide rounded-full bg-primary" />
            </div>
          ) : (
            <Progress
              value={barValue}
              className="mb-3"
              aria-label={t('playground.loadingProgress')}
              data-testid="playground-loading-progress"
            />
          )}

          {/* Issue#274：阶段条（stagesView 存在才渲染；否则布局逐字节不变——AC6 零回归面） */}
          {stageBarRows.length > 0 && (
            <p className="mb-3 text-xs text-muted-foreground" data-testid="playground-loading-stages">
              {stageBarRows.map((row, idx) => (
                <span key={row.key}>
                  {idx > 0 && <span aria-hidden="true"> → </span>}
                  {row.done && (
                    <Check aria-hidden="true" className="mr-1 inline size-3 text-emerald-600 dark:text-emerald-400" />
                  )}
                  <span
                    data-testid={`playground-loading-stage-${row.key}`}
                    className={cn(
                      row.done ? 'text-muted-foreground/80' : 'font-medium text-foreground',
                    )}
                  >
                    {row.label}
                  </span>
                  {!row.done && row.hint && (
                    <span aria-hidden="true"> · </span>
                  )}
                  {!row.done && row.hint && <span>{row.hint}</span>}
                </span>
              ))}
              {/* 并行加速器段（②'）：running 时附注「不影响逐页进度」 */}
              {stagesView?.acceleratorRunning && (
                <span>
                  <span aria-hidden="true"> · </span>
                  <span data-testid="playground-loading-stage-accelerator-running">
                    {t('playground.taskStage.acceleratorHint')}
                  </span>
                </span>
              )}
              {etaSuffix && (
                <span>
                  <span aria-hidden="true"> · </span>
                  <span data-testid="playground-loading-eta" className="tabular-nums">
                    {etaSuffix}
                  </span>
                </span>
              )}
            </p>
          )}

          {/* 段名 + 已运行（AC3/AC4）：段编号只在两段式出现；时长以后端 started_at 起算 */}
          <p className="mb-3 text-xs tabular-nums text-muted-foreground">
            {segmentLabel && (
              <>
                <span data-testid="playground-loading-stage">{segmentLabel}</span>
                <span aria-hidden="true"> · </span>
              </>
            )}
            <span data-testid="playground-loading-elapsed">{elapsedLabel}</span>
          </p>

          {/* 用户验收④：✓ 已完成清单——固定顺序、排除进行中段、不跳动 */}
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

          {children && <div className="mt-3">{children}</div>}
        </div>
      </div>,
      document.body,
    );
  },
);
