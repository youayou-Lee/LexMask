/**
 * Issue#338：解析期剔除的内嵌平铺水印行数 → 一句显式提示。
 *
 * 纯函数（无 React 依赖），把「脏数据口径」从 hook 里独立出来，便于直接钉住：
 * 只有**有限正整数**才出提示；0 / 负数 / 非整数 / NaN / Infinity / 字符串 / 布尔 /
 * null / undefined 一律 `null`（「没测到」与「0 行」都不是水印提醒的场合）。
 *
 * 后端真源 `ParseResult.watermark_inline_filtered`（单位=行，int）；
 * 这里仍按 unknown 防御——历史缓存/异常上游可能给出字符串或浮点。
 */
export function inlineWatermarkNoticeText(
  lines: unknown,
  translate: (key: string) => string,
): string | null {
  if (typeof lines !== 'number' || !Number.isInteger(lines) || lines <= 0) return null;
  return translate('playground.inlineWatermarkNotice').replace('{count}', String(lines));
}