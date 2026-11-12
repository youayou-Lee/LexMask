// Copyright 2026 LexMask Contributors

// Issue#274：阶段状态机前端纯函数——stages 解析 / 乐观①门控 / ETA 保守版。
//
// 数据源（轮询已带回的 item.performance，performance:true）：
// - recognition.stages = [{name, started_at, finished_at, state, detail}]
//   （backend task_queue_pipelines._stage_begin/_stage_end 落账）；
// - recognition.pages.<p>.stage_duration_ms = {backend段标签: ms}（F2 增量键）
//   与 cache_status.vision_result（ETA 样本源 + 缓存页剔除）；
// - recognition.current_segment.{label, concurrency}（当前段标签/并发）。
//
// 设计约束（方案 v3）：ETA 保守版 = 有效样本 p75 + 剔除缓存命中页 + 严格单调
// min(重算值, 上次显示值)（F10：禁止任何回弹）；样本 <3 只显示「约 N 秒/页」。
// 乐观①门控（F1 + v3 注记 1）：progress_stage==='vision' && current===0 &&
// 无 recognition.finished_at —— parse/重跑窗口不得误显「① 文档预检中」。

import type { Perf } from './task-progress';

export type StageRow = {
  name: string;
  started_at: string | null;
  finished_at: string | null;
  state: string;
  detail: Record<string, unknown>;
};

export type StagesView = {
  /** 当前 running 段名（无 running 行 → null）；并行加速器不算 current（§2.4/F8）。 */
  current: string | null;
  /** 已终态段名（按账序，不含 current）。 */
  done: string[];
  /** 加速器段在跑（②' 行标注「不影响逐页进度」）。 */
  acceleratorRunning: boolean;
  /** 保守 ETA（秒）；样本不足/无 running page_loop → null。 */
  etaSec: number | null;
  /** 「约 N 秒/页」；无有效样本 → null。 */
  perPageSec: number | null;
};

export type EtaSample = {
  durationMs: number;
  /** cache_status.vision_result === 'hit' → 剔除（防速率虚低）。 */
  cacheHit: boolean;
  /** 页账 stage_duration_ms 的段标签（backend stage_label）。 */
  stageLabel: string | null;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

// ---------------------------------------------------------------------------
// stages 解析
// ---------------------------------------------------------------------------

/** performance.recognition.stages → StageRow[]；缺键/坏形（旧任务）→ null；清账空数组 → []。 */
export function stagesFromPerformance(perf: Perf | null | undefined): StageRow[] | null {
  const raw: unknown = asRecord(asRecord(perf).recognition).stages;
  if (!Array.isArray(raw)) return null;
  const rows: StageRow[] = [];
  for (const entry of raw) {
    if (!isRecord(entry) || typeof entry.name !== 'string' || typeof entry.state !== 'string') {
      return null;
    }
    rows.push({
      name: entry.name,
      started_at: typeof entry.started_at === 'string' ? entry.started_at : null,
      finished_at: typeof entry.finished_at === 'string' ? entry.finished_at : null,
      state: entry.state,
      detail: asRecord(entry.detail),
    });
  }
  return rows;
}

/** recognition.finished_at 是否在账（重跑角例门控用，v3 注记 1）。 */
export function hasRecognitionFinishedAt(perf: Perf | null | undefined): boolean {
  const finishedAt = asRecord(asRecord(perf).recognition).finished_at;
  return typeof finishedAt === 'string' && finishedAt !== '';
}

// ---------------------------------------------------------------------------
// 乐观①门控
// ---------------------------------------------------------------------------

/**
 * 乐观「① 文档预检中」渲染条件（四条同时成立）：
 * 1. 非文本件（textMode 无页级语义）；
 * 2. progress_stage === 'vision'（parse/model 段不写 progress ⇒ parse 窗口为
 *    null/''/'ner' 等 ⇒ 不显示①——文档解析不是文档预检，AC6-parse-a）；
 * 3. progress_current === 0（已有页完成 → 以账为准）；
 * 4. 无 recognition.finished_at（重跑 parse 窗口的旧残账守卫，v3 注记 1）+
 *    stages 账为空（账已接管就不需要乐观）。
 */
export function optimisticPreflightEligible(params: {
  textMode: boolean;
  progressStage: string | null | undefined;
  progressCurrent: number;
  hasRecognitionFinishedAt: boolean;
  hasStageRows: boolean;
}): boolean {
  if (params.textMode) return false;
  if (params.progressStage !== 'vision') return false;
  if (params.progressCurrent !== 0) return false;
  if (params.hasRecognitionFinishedAt) return false;
  if (params.hasStageRows) return false;
  return true;
}

// ---------------------------------------------------------------------------
// ETA（AC5）
// ---------------------------------------------------------------------------

/** nearest-rank p75。 */
function percentile75(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const idx = Math.max(0, Math.ceil(0.75 * sorted.length) - 1);
  return sorted[idx] ?? 0;
}

/** 中位数（偶数取中间均值）。 */
function median(values: number[]): number {
  if (values.length === 0) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  if (sorted.length % 2 === 1) return sorted[mid]!;
  return ((sorted[mid - 1] ?? 0) + (sorted[mid] ?? 0)) / 2;
}

/**
 * ETA 纯计算（stagesViewFromPerformance 与测试共用同一实现）。
 * - 有效样本 = 非缓存命中 且 stageLabel === currentStageLabel（v2/F2：两段式
 *   第二段不得混入第一段样本）；
 * - 样本 <3 ⇒ 无总 ETA，只给「约 N 秒/页」（中位数 ceil，下限 1s）；
 * - 样本 ≥3 ⇒ ETA = ceil(剩余页 × p75 / 并发)；
 * - 严格单调：min(重算值, previousEtaSec)（v2/F10，禁止回弹）；
 * - completed ≥ total ⇒ 双双消失。
 */
export function computeEtaFromSamples(
  samples: EtaSample[],
  opts: {
    completed: number;
    total: number;
    concurrency: number;
    previousEtaSec: number | null;
    currentStageLabel?: string | null;
  },
): { etaSec: number | null; perPageSec: number | null } {
  const remaining = Math.max(0, Math.max(0, opts.total) - Math.max(0, opts.completed));
  if (opts.total > 0 && remaining === 0) return { etaSec: null, perPageSec: null };
  const valid = samples.filter(
    (s) =>
      !s.cacheHit &&
      s.durationMs >= 0 &&
      (opts.currentStageLabel == null || s.stageLabel === opts.currentStageLabel),
  );
  if (valid.length === 0) return { etaSec: null, perPageSec: null };
  const perPageSec = Math.max(1, Math.ceil(median(valid.map((s) => s.durationMs)) / 1000));
  if (valid.length < 3) return { etaSec: null, perPageSec };
  const p75 = percentile75(valid.map((s) => s.durationMs));
  const concurrency = Math.max(1, Math.floor(opts.concurrency));
  let etaSec = Math.ceil((remaining * p75) / concurrency / 1000);
  if (opts.previousEtaSec != null && opts.previousEtaSec > 0) {
    etaSec = Math.min(etaSec, opts.previousEtaSec);
  }
  return { etaSec, perPageSec };
}

/** 页账 → ETA 样本（stage_duration_ms 段标签 + cache_status）。坏形条目跳过。 */
export function etaSamplesFromPerformance(perf: Perf | null | undefined): EtaSample[] {
  const pages = asRecord(asRecord(asRecord(perf).recognition).pages);
  const samples: EtaSample[] = [];
  for (const entry of Object.values(pages)) {
    if (!isRecord(entry)) continue;
    const stageMs = asRecord(entry.stage_duration_ms);
    const cacheStatus = asRecord(entry.cache_status);
    const durationMs = typeof entry.duration_ms === 'number' ? entry.duration_ms : null;
    const labels = Object.keys(stageMs).filter(
      (k) => typeof stageMs[k] === 'number' && (stageMs[k] as number) >= 0,
    );
    if (labels.length === 0) {
      if (durationMs != null) {
        samples.push({
          durationMs,
          cacheHit: cacheStatus.vision_result === 'hit',
          stageLabel: null,
        });
      }
      continue;
    }
    for (const label of labels) {
      samples.push({
        durationMs: stageMs[label] as number,
        cacheHit: cacheStatus.vision_result === 'hit',
        stageLabel: label,
      });
    }
  }
  return samples;
}

/** 当前段标签 + 并发（recognition.current_segment）；缺段信息 → (null, 1)。 */
export function currentSegmentRuntime(perf: Perf | null | undefined): {
  label: string | null;
  concurrency: number;
} {
  const seg = asRecord(asRecord(asRecord(perf).recognition).current_segment);
  const label = typeof seg.label === 'string' && seg.label !== '' ? seg.label : null;
  const concurrency = typeof seg.concurrency === 'number' && seg.concurrency >= 1 ? Math.floor(seg.concurrency) : 1;
  return { label, concurrency };
}

// ---------------------------------------------------------------------------
// 视图模型
// ---------------------------------------------------------------------------

/**
 * stages 账 → 组件视图模型。无账/空账 → null（调用方回落乐观门控/现状渲染）。
 * ETA 只在 page_loop running 且样本足够时计算（preflight 本身 ≤~3s，不给 ETA）。
 */
export function stagesViewFromPerformance(
  perf: Perf | null | undefined,
  opts: { completed: number; total: number; previousEtaSec: number | null },
): StagesView | null {
  const rows = stagesFromPerformance(perf);
  if (rows === null || rows.length === 0) return null;
  // 门⑥ M-4：done 只收 completed——failed/skipped 段不得渲染成「✓ 已完成」
  //（诚实显示；该态在轮询上几乎不可达——item FAILED 先写、loading 先退——纯防御）。
  const done = rows.filter((r) => r.state === 'completed').map((r) => r.name);
  const running = rows.filter((r) => r.state === 'running');
  const acceleratorRunning = running.some((r) => r.name === 'accelerator');
  const primary = running.find((r) => r.name !== 'accelerator') ?? null;
  let etaSec: number | null = null;
  let perPageSec: number | null = null;
  if (primary?.name === 'page_loop') {
    const { label, concurrency } = currentSegmentRuntime(perf);
    const eta = computeEtaFromSamples(etaSamplesFromPerformance(perf), {
      completed: opts.completed,
      total: opts.total,
      concurrency,
      previousEtaSec: opts.previousEtaSec,
      currentStageLabel: label,
    });
    etaSec = eta.etaSec;
    perPageSec = eta.perPageSec;
  }
  return {
    current: primary?.name ?? null,
    done,
    acceleratorRunning,
    etaSec,
    perPageSec,
  };
}

/** 段名 → i18n 标签键（组件用 t() 渲染；未知段名不渲染标签行）。 */
export const STAGE_LABEL_KEYS: Record<string, string> = {
  preflight: 'playground.taskStage.preflight',
  page_loop: 'playground.taskStage.pageLoop',
  accelerator: 'playground.taskStage.accelerator',
};

/** 段名 → 一句话说明 i18n 键。 */
export const STAGE_HINT_KEYS: Record<string, string> = {
  preflight: 'playground.taskStage.preflightHint',
  page_loop: 'playground.taskStage.pageLoopHint',
  accelerator: 'playground.taskStage.acceleratorHint',
};
