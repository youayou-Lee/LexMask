// Copyright 2026 LexMask Contributors

/**
 * 后端错误信封的取错工具（#232 M-3 收口：文案提取全站一个具名函数）。
 *
 * 2026-11-03 用户反馈的根因：旧实现 `data.detail ?? data.message` 被空对象
 * `detail:{}` 短路（`??` 只挡 nullish），message 里的具体原因永远取不到。
 * 这里是文案提取的单一权威实现；playground / agent-md / 下载路径一律复用，
 * 禁止再写内联取法（评审 M-4）。
 *
 * 豁免（门⑥ M-1 留痕，2026-11-04）：①按码路由类解析器不在此列——
 * `use-playground-file.readErrorEnvelope` / `agent-md/api.parseUploadErrorEnvelope`
 * 的职责是取 error_code 路由交互（PDF 密码框），顺带取文案但字段顺序独立维护；
 * ②`dicomApi` 的 `error.message ?? error.detail` 属另一族（两路径均产不出
 * FILE_CONTENT_MISMATCH，纯文案提取无行为差异），后续动到时再收口。
 */

/** 返回第一个非空字符串候选（trim 后），全空时 null。 */
export function firstMessageText(...candidates: unknown[]): string | null {
  for (const candidate of candidates) {
    if (typeof candidate === 'string' && candidate.trim()) return candidate.trim();
  }
  return null;
}

export type ErrorEnvelopeLike = {
  detail?: unknown;
  message?: unknown;
  error?: unknown;
};

/**
 * 从已解析的信封对象取用户可读文案。
 *
 * - 字符串字段按 detail→message→error 取第一个非空；
 * - 非空结构化 detail（如 422 校验数组）保留 stringify 兜底；
 * - 全空（含空对象 detail）回退 fallback——不再渲染 "{}"。
 */
export function errorEnvelopeMessage(
  data: ErrorEnvelopeLike | null | undefined,
  fallback: string,
): string {
  const text = firstMessageText(data?.detail, data?.message, data?.error);
  if (text) return text;
  const detail = data?.detail;
  if (detail && typeof detail === 'object' && Object.keys(detail as object).length > 0) {
    return JSON.stringify(detail);
  }
  return fallback;
}

/** 从响应体信封里取机读错误码（无码/非字符串返回 null）。 */
export function errorCodeFromEnvelope(data: ErrorEnvelopeLike | null | undefined): string | null {
  const code = (data as { error_code?: unknown } | null | undefined)?.error_code;
  return typeof code === 'string' && code ? code : null;
}
