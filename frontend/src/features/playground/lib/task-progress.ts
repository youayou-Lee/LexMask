// Copyright 2026 LexMask Contributors

// Issue#194 用户验收③：视觉管线是两段式——先 OCR 文字识别一遍 N 页，再视觉要素
// 一遍 N 页（task_queue_pipelines.py 两处 run_page_stage），每页进度消息
// `{stage_label} page {p}/{pages}`。两轮进度数字 0→N 完全相同，前端只显示数字时
// 用户把第二段看成「重新开始」。这里把服务端 message 映射成人话段名（i18n 键，
// zh/en 双语），数字保留；不要直接秀英文/机翻原文。
//
// 映射表写死对照后端 stage_label 与 message 的真实取值（勿凭空造）：
// - run_page 默认形参 stage_label="视觉识别"（pipeline.py:306）
// - 编排器两段调用："OCR+HaS识别"（:488）、"视觉特征识别"（:497）
// - 文本件 NER 段：message="text_recognition_running"（stage=ner current=total=1）
// - 无段名消息（"Vision recognition queued for N page(s)"、"vision_recognition_complete"、
//   parse 段）→ null，调用方回落既有「后台识别中 current/total」文案。

export type RecognitionStage = {
  /** i18n 键（playground.taskStage.*，zh/en 同键） */
  labelKey: string;
  /** 消息里携带的页码（与 item.progress_current/total 同源同值，优先用消息自带对） */
  current: number;
  total: number;
};

/** 逐页消息形状：`{stage_label} page {p}/{pages}`（整串匹配，防前后缀注入误映射）。 */
const PAGE_MESSAGE_PATTERNS: Array<[RegExp, string]> = [
  [/^OCR\+HaS识别 page (\d+)\/(\d+)$/, 'playground.taskStage.ocrHas'],
  [/^视觉特征识别 page (\d+)\/(\d+)$/, 'playground.taskStage.visualFeatures'],
  [/^视觉识别 page (\d+)\/(\d+)$/, 'playground.taskStage.vision'],
  // Issue#238：打码段逐页消息（task_queue_pipelines 打码回调写入）——被动小卡取数
  [/^打码 page (\d+)\/(\d+)$/, 'playground.taskStage.redaction'],
];

/** 后端识别进度 message → 人话段名 + 页码；未知/无段名消息 → null。 */
export function recognitionStageFromMessage(
  message: string | null | undefined,
): RecognitionStage | null {
  if (!message) return null;
  for (const [pattern, labelKey] of PAGE_MESSAGE_PATTERNS) {
    const match = pattern.exec(message);
    if (match) {
      return { labelKey, current: Number(match[1]), total: Number(match[2]) };
    }
  }
  if (message === 'text_recognition_running') {
    return { labelKey: 'playground.taskStage.text', current: 1, total: 1 };
  }
  return null;
}

// ---------------------------------------------------------------------------
// Issue#238：进度卡诚实化取数纯函数——数据全部来自轮询已带回的 item performance。
// ---------------------------------------------------------------------------

type Perf = Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

/** recognition.current_segment 形状（task_queue_pipelines 每段起跑覆写）。 */
export type SegmentInfo = {
  index: number;
  total: number;
  /** 该段实际页并发（合并段默认 1=串行）；缺失/非法 → null（不显示「并行处理中」） */
  concurrency: number | null;
};

/** performance.recognition.current_segment → 段信息；缺键/坏形 → null（单段旧任务回落）。 */
export function currentSegmentFromPerformance(perf: Perf | null | undefined): SegmentInfo | null {
  const seg = asRecord(asRecord(asRecord(perf).recognition).current_segment);
  const index = Number(seg.index);
  const total = Number(seg.total);
  if (!Number.isFinite(index) || !Number.isFinite(total) || index < 1 || total < 1) return null;
  const concurrency = Number(seg.concurrency);
  return {
    index,
    total,
    concurrency: Number.isFinite(concurrency) && concurrency > 0 ? concurrency : null,
  };
}

/**
 * 「并行处理中」页数 = min(该段页并发, 剩余页)。信号量模型下在飞页数恰为该值；
 * 段并发 <=1（串行）或缺段信息 → null（不显示）。
 */
export function inFlightPages(
  segment: SegmentInfo | null,
  completed: number,
  total: number,
): number | null {
  if (!segment || segment.concurrency === null || segment.concurrency <= 1) return null;
  const remaining = Math.max(0, Math.max(0, total) - Math.max(0, completed));
  return Math.min(segment.concurrency, remaining);
}

/** 任务段 started_at（ISO）——已运行时长以后端起算，刷新不走表；缺键 → null。 */
export function stageStartedAtFromPerformance(
  perf: Perf | null | undefined,
  stage: 'recognition' | 'redaction',
): string | null {
  const startedAt = asRecord(asRecord(perf)[stage]).started_at;
  return typeof startedAt === 'string' && startedAt !== '' ? startedAt : null;
}
