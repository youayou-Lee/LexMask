// Copyright 2026 LexMask Contributors

// Issue#236：水印预处理的界面读法。
//
// 后端 `pipeline_status.watermark_overlay` 的形状（页级复制）：
//   { ran, skipped, failed, code, source, pages_probed, pages_hit,
//     pages_stripped, pages_skipped, pages_guard_refused, duration_ms, region_count, error }
// 其中 `region_count` 恒为 0（这不是「打了多少个区域」，别拿它当判据），
// 真正的证据是 `pages_stripped`（成功摘除的水印页数）。

export type WatermarkOverlayStatus = Record<string, unknown>;

const asRecord = (value: unknown): Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};

/**
 * 从文件详情 `vision_quality`（页号字符串键 → 页记录）里取第一页的
 * `watermark_overlay` 状态。文档级状态在每页各存了一份，取任意页等价；
 * 取不到时返回 null（旧文件 / 未跑识别 / 非 PDF）。
 */
export function watermarkOverlayStatusFromVisionQuality(
  visionQuality: unknown,
): WatermarkOverlayStatus | null {
  const byPage = asRecord(visionQuality);
  for (const page of Object.values(byPage)) {
    const status = asRecord(asRecord(page)['pipeline_status'])['watermark_overlay'];
    if (Object.keys(asRecord(status)).length > 0) return asRecord(status);
  }
  return null;
}

/**
 * 水印预处理 chip 文案；返回 null 表示走通用状态行（未命中/未跑/关闭都不该
 * 谎报「已摘除」）。
 *
 * - `code=hit` 且 `source=copy` 且有 `pages_stripped` 数 ⇒「已摘除 N 页」
 * - `code=guard_refused_pages` ⇒「已摘除 N 页」但留痕说明部分页未摘净
 */
export function watermarkStripChipText(
  t: (key: string) => string,
  status: unknown,
): string | null {
  const s = asRecord(status);
  const code = s['code'];
  const source = s['source'];
  const stripped = s['pages_stripped'];

  if (typeof stripped !== 'number' || !Number.isFinite(stripped) || stripped <= 0) return null;
  if (source !== 'copy') return null;
  if (code !== 'hit' && code !== 'guard_refused_pages') return null;

  return t('batchWizard.step4.pipelineWatermarkStripped').replace('{pages}', String(stripped));
}