// Copyright 2026 LexMask Contributors

// Issue#244：水印工具的纯函数层——code 人话映射键、上传弹窗三态判定、
// 净化件下载文件名解析。全部无副作用（vitest node 环境可测），
// 组件逻辑只消费这里的判定结果，不在 JSX 里混业务规则。

/** watermark-status / watermark-strip 报告里的机读 code（与后端 watermark_overlay 对齐）。 */
export type WatermarkStatusCode =
  | 'hit'
  | 'no_wm_spans'
  | 'no_isolated_overlay'
  | 'below_min_pages'
  | 'encrypted'
  | 'capped_by_size'
  | 'not_pdf'
  | 'not_applicable'
  | 'disabled'
  | 'error';

const WATERMARK_STATUS_CODES: readonly WatermarkStatusCode[] = [
  'hit',
  'no_wm_spans',
  'no_isolated_overlay',
  'below_min_pages',
  'encrypted',
  'capped_by_size',
  'not_pdf',
  'not_applicable',
  'disabled',
  'error',
];

export function isWatermarkStatusCode(value: unknown): value is WatermarkStatusCode {
  return typeof value === 'string' && (WATERMARK_STATUS_CODES as readonly string[]).includes(value);
}

/** code → i18n 键（`watermarkTool.code.*`）；未知 code 兜底 unknown 键，不猜不吞。 */
export function watermarkCodeTextKey(code: unknown): string {
  return isWatermarkStatusCode(code) ? `watermarkTool.code.${code}` : 'watermarkTool.code.unknown';
}

/** 后端 file_type 的 PDF 家族（∈ {pdf, pdf_scanned}）——水印检测只对 PDF 有意义。 */
export function isPdfFamilyFileType(fileType: unknown): boolean {
  return fileType === 'pdf' || fileType === 'pdf_scanned';
}

/** 上传弹窗的提示形态：'hit' = 自动去水印预告；'unstrip' = 无法自动去除警示；null = 静默。 */
export type WatermarkUploadNotice = 'hit' | 'unstrip' | null;

export interface WatermarkUploadToastPlan {
  notice: WatermarkUploadNotice;
  /** i18n 键；notice 为 null 时也为 null。hit 文案含 {pages} 占位。 */
  messageKey: string | null;
  /** pages_hit 快照；非数值时为 null（文案侧按缺省渲染）。 */
  pages: number | null;
}

/**
 * 上传弹窗三态判定（Issue#244 任务 B）：输入 watermark-status 响应，输出 toast 形态。
 * - code=hit → 'hit'（N = pages_hit）
 * - code ∈ {no_isolated_overlay, below_min_pages} → 'unstrip'
 * - 其他 code / 结构不合法 → null（静默，弹窗永不阻断上传流程）
 */
export function watermarkUploadToastPlan(status: unknown): WatermarkUploadToastPlan {
  const code =
    typeof status === 'object' && status !== null ? (status as { code?: unknown }).code : undefined;
  if (code === 'hit') {
    const rawPages =
      typeof status === 'object' && status !== null
        ? (status as { pages_hit?: unknown }).pages_hit
        : undefined;
    const pages =
      typeof rawPages === 'number' && Number.isFinite(rawPages) && rawPages > 0
        ? rawPages
        : null;
    return { notice: 'hit', messageKey: 'watermarkTool.uploadHitToast', pages };
  }
  if (code === 'no_isolated_overlay' || code === 'below_min_pages') {
    return { notice: 'unstrip', messageKey: 'watermarkTool.uploadUnstripToast', pages: null };
  }
  return { notice: null, messageKey: null, pages: null };
}

/**
 * 从 Content-Disposition 头取下载文件名：优先 RFC 5987 `filename*=UTF-8''…`，
 * 回退 `filename="…"`；都取不到返回 null（调用方走本地化兜底名）。
 */
export function filenameFromContentDisposition(
  header: string | null | undefined,
): string | null {
  if (!header) return null;
  const starMatch = /filename\*\s*=\s*UTF-8''([^;]+)/i.exec(header);
  if (starMatch?.[1]) {
    try {
      return decodeURIComponent(starMatch[1].trim().replace(/^"|"$/g, '')) || null;
    } catch {
      // 非法百分号编码：落到 filename= 分支
    }
  }
  const plainMatch = /filename\s*=\s*"([^"]*)"/i.exec(header);
  if (plainMatch) return plainMatch[1] || null;
  const bareMatch = /filename\s*=\s*([^;]+)/i.exec(header);
  if (!bareMatch?.[1]) return null;
  const bare = bareMatch[1].trim().replace(/^"|"$/g, '');
  return bare || null;
}

/** 后端 strip 报告 → 工具页结果摘要行的字段守卫读取（缺字段显示「—」，不谎报 0）。 */
export function stripSummaryFields(result: unknown): {
  pagesHit: number | null;
  pagesStripped: number | null;
  pagesSkipped: number | null;
  guardRefused: number | null;
  durationMs: number | null;
  cleanAvailable: boolean;
} {
  const r =
    typeof result === 'object' && result !== null ? (result as Record<string, unknown>) : {};
  const num = (key: string): number | null => {
    const value = r[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  };
  return {
    pagesHit: num('pages_hit'),
    pagesStripped: num('pages_stripped'),
    pagesSkipped: num('pages_skipped'),
    guardRefused: num('pages_guard_refused'),
    durationMs: num('duration_ms'),
    cleanAvailable: r['clean_available'] === true,
  };
}
