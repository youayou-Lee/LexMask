// Copyright 2026 LexMask Contributors

// Issue#194 Task 11：处理详情行构建纯函数（spec §5 映射表逐字落地）。
//
// 输入 `perf` 是单文件任务 item 的 performance（getJob(jobId,{performance:true})）。
// 后端真实键形（写死，勿凭空造）：
// - 原始工人写入（task_queue_pipelines.py / task_queue.py）：
//   recognition.queue_wait_ms、queue.last_wait_ms、recognition.parse_ms/ner_ms/
//   mode/page_count/duration_ms、recognition.pages 按字符串页号键控（页记录含
//   status: completed|failed|timeout 与 duration_breakdown_ms）、redaction.duration_ms/
//   entity_count/bounding_box_count；
// - API 别名（jobs.py，T6 契约）：item 顶层 queue_wait_ms / recognition_duration_ms /
//   redaction_duration_ms / recognition_pages（页记录数组）——本函数三种来源都认，
//   组件传原始 performance、任务中心传别名合并形状，产出一致（三处一致性的根基）；
// - duration_breakdown_ms（task_queue_metrics._duration_breakdown_from_quality）：
//   VisionResult.duration_ms 原样（顶层 ocr_has / visual_features / pdf_render_ms）+
//   pipeline_status.*.stage_duration_ms 拍平为 "前缀.键"（ocr_has.ocr /
//   ocr_has.has_ner / ocr_has.match）。OCR 行同时累计两种键形。
//
// 空值规则：阶段键缺失 → 整行不显示；全空 → []（组件据此不渲染卡，AC3 旧文件条款）。

import { t } from '@/i18n';

export type ProcessingRowKey =
  | 'queue_wait'
  | 'parse'
  | 'ocr'
  | 'match'
  | 'ner'
  | 'visual_features'
  | 'redaction'
  | 'failed_chunks';

export type ProcessingRow = {
  key: ProcessingRowKey;
  labelKey: string;
  durationMs: number | null;
  counts: string | null;
  tone?: 'warn';
};

type Perf = Record<string, unknown>;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

/** 宽松取数：数字（有限）直取；数字字符串转换；其余 null。后端数值可能序列化为字符串。 */
function num(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

/** 页记录列表：兼容工人形状（字符串页号键控记录）与 API 别名形状（数组）。 */
function pageEntries(perf: Perf): Record<string, unknown>[] {
  const recognition = asRecord(perf.recognition);
  const raw = perf.recognition_pages ?? recognition.pages;
  if (Array.isArray(raw)) {
    return raw.filter(isRecord);
  }
  if (isRecord(raw)) {
    return Object.values(raw).filter(isRecord);
  }
  return [];
}

/** 页级 breakdown 的 OCR 计时：拍平键（ocr_has.ocr）或裸键（ocr）。 */
function pageOcrMs(page: Record<string, unknown>): number | null {
  const breakdown = asRecord(page.duration_breakdown_ms);
  const values = [breakdown['ocr_has.ocr'], breakdown['ocr']].map(num);
  const found = values.filter((value): value is number => value !== null);
  return found.length > 0 ? found.reduce((sum, value) => sum + value, 0) : null;
}

function pageMatchMs(page: Record<string, unknown>): number | null {
  const breakdown = asRecord(page.duration_breakdown_ms);
  const values = [breakdown['ocr_has.match'], breakdown['match']].map(num);
  const found = values.filter((value): value is number => value !== null);
  return found.length > 0 ? found.reduce((sum, value) => sum + value, 0) : null;
}

function pageNerMs(page: Record<string, unknown>): number | null {
  const breakdown = asRecord(page.duration_breakdown_ms);
  const values = [breakdown['ocr_has.has_ner'], breakdown['has_ner']].map(num);
  const found = values.filter((value): value is number => value !== null);
  return found.length > 0 ? found.reduce((sum, value) => sum + value, 0) : null;
}

/** 页级 visual_features 总时长（VisionResult.duration_ms 顶层键，页级管线合计）。 */
function pageVisualFeaturesMs(page: Record<string, unknown>): number | null {
  const value = num(asRecord(page.duration_breakdown_ms)['visual_features']);
  return value;
}

/** 页级 pdf_render_ms：breakdown 顶层（真实形状）或 pipeline_status.pdf_render 段（容错）。 */
function pagePdfRenderMs(page: Record<string, unknown>): number | null {
  const fromBreakdown = num(asRecord(page.duration_breakdown_ms)['pdf_render_ms']);
  if (fromBreakdown !== null) return fromBreakdown;
  return num(asRecord(asRecord(page.pipeline_status)['pdf_render'])['duration_ms']);
}

/** 失败回落页数：status 存在且非成功值（completed；兼容 ok）即计入。 */
function failedPageCount(pages: Record<string, unknown>[]): number {
  return pages.filter((page) => {
    const status = page.status;
    if (typeof status !== 'string' || status === '') return false;
    return status !== 'completed' && status !== 'ok';
  }).length;
}

function pagesCountLabel(count: number): string {
  return t('processingDetails.pages').replace('{n}', String(count));
}

function fill(template: string, params: Record<string, string>): string {
  return Object.entries(params).reduce(
    (acc, [name, value]) => acc.split(`{${name}}`).join(value),
    template,
  );
}

export function formatDurationMs(ms: number): string {
  if (ms < 1_000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) {
    const seconds = Math.round((ms / 1_000) * 10) / 10;
    return `${seconds}s`;
  }
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1_000);
  return `${minutes}m ${seconds}s`;
}

/** 文本直读判定：workers 文本流写 recognition.mode==='text'（无 pages）。 */
export function isTextDirectMode(perf: Perf | null | undefined): boolean {
  return asRecord(asRecord(perf).recognition).mode === 'text';
}

/**
 * 文本件的 page_count 组件层合并：workers 文本流不写 page_count，spec 要求
 * counts=page_count 时取 GET /files/{id} 的 page_count 补充——调用方（组件）把
 * 文件记录页数传进来，后端已写的 recognition.page_count 优先。
 * 返回新对象，不修改入参；无补充值时原样返回。
 */
export function mergeFilePageCount(
  perf: Perf | null | undefined,
  filePageCount: number | null | undefined,
): Perf | null {
  if (!isRecord(perf)) return null;
  if (typeof filePageCount !== 'number' || !Number.isFinite(filePageCount) || filePageCount <= 0) {
    return perf;
  }
  const recognition = asRecord(perf.recognition);
  const existing = num(recognition.page_count);
  if (existing !== null && existing > 0) return perf;
  return { ...perf, recognition: { ...recognition, page_count: filePageCount } };
}

/**
 * spec §5 映射表：performance → 阶段行（键序固定）。
 * 阶段键缺失 → 行不显示；全空 → []（旧文件不渲染卡）。
 */
export function buildProcessingRows(perf: Perf | null | undefined): ProcessingRow[] {
  const record = isRecord(perf) ? perf : {};
  const recognition = asRecord(record.recognition);
  const redaction = asRecord(record.redaction);
  const queue = asRecord(record.queue);
  const rows: ProcessingRow[] = [];

  // 排队等待：顶层别名 > recognition.queue_wait_ms > queue.last_wait_ms（后端同值同写）。
  const queueWaitMs =
    num(record.queue_wait_ms) ?? num(recognition.queue_wait_ms) ?? num(queue.last_wait_ms);
  if (queueWaitMs !== null) {
    rows.push({ key: 'queue_wait', labelKey: 'processingDetails.queueWait', durationMs: queueWaitMs, counts: null });
  }

  // 文本解析：recognition.parse_ms；counts=page_count。
  const parseMs = num(recognition.parse_ms);
  if (parseMs !== null) {
    const pageCount = num(recognition.page_count);
    rows.push({
      key: 'parse',
      labelKey: 'processingDetails.parse',
      durationMs: parseMs,
      counts: pageCount !== null && pageCount > 0 ? pagesCountLabel(pageCount) : null,
    });
  }

  // 文字识别(OCR)：Σ 页级 ocr 键 + Σ pdf_render_ms；文本件（mode==='text' 或无 pages）无此行。
  const pages = pageEntries(record);
  const isTextDirect = recognition.mode === 'text';
  if (!isTextDirect && pages.length > 0) {
    const ocrParts = pages.map(pageOcrMs).filter((value): value is number => value !== null);
    const pdfRenderParts = pages.map(pagePdfRenderMs).filter((value): value is number => value !== null);
    const ocrMs =
      (ocrParts.length > 0 ? ocrParts.reduce((sum, value) => sum + value, 0) : 0) +
      (pdfRenderParts.length > 0 ? pdfRenderParts.reduce((sum, value) => sum + value, 0) : 0);
    // counts=页数+均速；均速分母 0 兜底（pages 非空才进这里，分母恒 >0，缺 page_count 也按页记录数算）
    const counts = `${pagesCountLabel(pages.length)} · ${fill(t('processingDetails.perPage'), {
      s: formatDurationMs(Math.round(ocrMs / pages.length)),
    })}`;
    rows.push({ key: 'ocr', labelKey: 'processingDetails.ocr', durationMs: ocrMs, counts });
  }

  // 实体匹配：Σ 页级 match；无键不显示。
  const matchParts = pages.map(pageMatchMs).filter((value): value is number => value !== null);
  if (matchParts.length > 0) {
    rows.push({
      key: 'match',
      labelKey: 'processingDetails.match',
      durationMs: matchParts.reduce((sum, value) => sum + value, 0),
      counts: null,
    });
  }

  // 实体识别(NER)：recognition.ner_ms（文本流）优先，缺失 Σ 页级 has_ner；都无不显示。
  const nerMs = num(recognition.ner_ms);
  const nerParts = pages.map(pageNerMs).filter((value): value is number => value !== null);
  if (nerMs !== null) {
    rows.push({ key: 'ner', labelKey: 'processingDetails.ner', durationMs: nerMs, counts: null });
  } else if (nerParts.length > 0) {
    rows.push({
      key: 'ner',
      labelKey: 'processingDetails.ner',
      durationMs: nerParts.reduce((sum, value) => sum + value, 0),
      counts: null,
    });
  }

  // 视觉要素：Σ 页级 visual_features；无键不显示。
  const visualParts = pages
    .map(pageVisualFeaturesMs)
    .filter((value): value is number => value !== null);
  if (visualParts.length > 0) {
    rows.push({
      key: 'visual_features',
      labelKey: 'processingDetails.visualFeatures',
      durationMs: visualParts.reduce((sum, value) => sum + value, 0),
      counts: null,
    });
  }

  // 打码渲染：redaction.duration_ms；counts=entity_count+bounding_box_count。
  const redactionMs = num(record.redaction_duration_ms) ?? num(redaction.duration_ms);
  if (redactionMs !== null) {
    const entityCount = num(redaction.entity_count);
    const boxCount = num(redaction.bounding_box_count);
    const countsParts: string[] = [];
    if (entityCount !== null && entityCount > 0) {
      countsParts.push(fill(t('processingDetails.entityCount'), { n: String(entityCount) }));
    }
    if (boxCount !== null && boxCount > 0) {
      countsParts.push(fill(t('processingDetails.boxCount'), { n: String(boxCount) }));
    }
    rows.push({
      key: 'redaction',
      labelKey: 'processingDetails.redaction',
      durationMs: redactionMs,
      counts: countsParts.length > 0 ? countsParts.join(' · ') : null,
    });
  }

  // 失败回落：非成功状态页计数；0 / 缺失不显示。
  const failedCount = failedPageCount(pages);
  if (failedCount > 0) {
    rows.push({
      key: 'failed_chunks',
      labelKey: 'processingDetails.failedChunks',
      durationMs: null,
      counts: fill(t('processingDetails.failedChunks'), { n: String(failedCount) }),
      tone: 'warn',
    });
  }

  return rows;
}

/** 总耗时 = 识别段（recognition.duration_ms）+ 打码段（redaction.duration_ms）；排队单列不并入。 */
export function buildProcessingTotalMs(perf: Perf | null | undefined): number | null {
  const record = isRecord(perf) ? perf : {};
  const recognition = asRecord(record.recognition);
  const redaction = asRecord(record.redaction);
  const recognitionMs = num(record.recognition_duration_ms) ?? num(recognition.duration_ms);
  const redactionMs = num(record.redaction_duration_ms) ?? num(redaction.duration_ms);
  if (recognitionMs === null && redactionMs === null) return null;
  return (recognitionMs ?? 0) + (redactionMs ?? 0);
}
