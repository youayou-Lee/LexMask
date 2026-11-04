// Copyright 2026 LexMask Contributors

import { t, useI18n } from '@/i18n';
import { firstMessageText } from '@/utils/errorEnvelope';

type ErrorLike = {
  message?: unknown;
  status?: unknown;
  /** fetchBlob 系 ApiError 携带的机读码与结构化 detail（#232 I-1）。 */
  errorCode?: unknown;
  detail?: unknown;
  response?: {
    status?: unknown;
    data?: {
      message?: unknown;
      detail?: unknown;
      error_code?: unknown;
    };
  };
};

/**
 * 后端错误信封的机器可读错误码（`app/core/errors.py` error_response）。
 * #30 起 PDF 加密类错误带码：调用方（如 playground 密码框）按码路由交互，
 * 文案映射只作 toast 兜底。无码返回 null，调用方回退字符串启发式。
 * #232：fetchBlob 系 ApiError 把码放在实例字段上，同样认。
 */
export function errorCodeFromError(error: unknown): string | null {
  const candidate = (error && typeof error === 'object' ? error : null) as ErrorLike | null;
  const code = candidate?.response?.data?.error_code;
  if (typeof code === 'string' && code) return code;
  const direct = candidate?.errorCode;
  return typeof direct === 'string' && direct ? direct : null;
}

/** 信封里的结构化 detail（ApiError 实例字段或 axios 形 response.data.detail）。 */
function errorDetailFromError(error: unknown): unknown {
  const candidate = (error && typeof error === 'object' ? error : null) as ErrorLike | null;
  if (candidate?.detail !== undefined) return candidate.detail;
  return candidate?.response?.data?.detail;
}

const ERROR_CODE_MESSAGE_KEYS: Record<string, string> = {
  PDF_ENCRYPTED_NEEDS_PASSWORD: 'common.pdfEncrypted',
  PDF_WRONG_PASSWORD: 'common.pdfWrongPassword',
  // #232 I-1：detail 缺失时的通用兜底；有 detail 时走 composeFileContentMismatch
  FILE_CONTENT_MISMATCH: 'common.fileContentMismatch',
};

/** FILE_CONTENT_MISMATCH 的机读 detail 形状（后端 spoof_reject_error）。 */
type ContentMismatchDetail = {
  detected_format?: unknown;
  extension?: unknown;
};

function mismatchDetailField(detail: unknown, field: keyof ContentMismatchDetail): string | null {
  if (!detail || typeof detail !== 'object') return null;
  const value = (detail as ContentMismatchDetail)[field];
  return typeof value === 'string' && value ? value : null;
}

/**
 * #232 I-1：按机读 detail 组合本地化拒收文案。
 * format 名走 i18n 词包（common.format*），模板与后端 zh 文案逐字对齐。
 */
function composeFileContentMismatch(detail: unknown): string {
  const formatCode = mismatchDetailField(detail, 'detected_format');
  const extension = mismatchDetailField(detail, 'extension');
  if (formatCode && extension) {
    const formatName = t(`common.format${formatCode.charAt(0).toUpperCase()}${formatCode.slice(1)}`);
    return t('common.fileContentMismatchWithFormat')
      .replace('{format}', formatName)
      .replace('{ext}', extension);
  }
  return t('common.fileContentMismatch');
}

/**
 * #232：信封 → 用户可读文案的统一入口（responseErrorMessage 系路径用）。
 * 已知码先映射本地化（en 用户不再看中文原文）；未知码保持原样取
 * 第一个非空字符串字段——全空回退 fallbackKey。
 */
export function messageFromEnvelope(
  data: { error_code?: unknown; detail?: unknown; message?: unknown; error?: unknown } | null,
  fallbackKey: string,
): string {
  const code = toText(data?.error_code);
  if (code === 'FILE_CONTENT_MISMATCH') {
    return composeFileContentMismatch(data?.detail);
  }
  if (code && ERROR_CODE_MESSAGE_KEYS[code]) {
    return t(ERROR_CODE_MESSAGE_KEYS[code]);
  }
  return firstMessageText(data?.detail, data?.message, data?.error) ?? t(fallbackKey);
}

function toText(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function extractStatus(raw: string, error?: ErrorLike): string | null {
  const explicit =
    typeof error?.status === 'number'
      ? String(error.status)
      : typeof error?.response?.status === 'number'
        ? String(error.response.status)
        : null;
  if (explicit) return explicit;

  const match = raw.match(/\b(\d{3})\b/);
  return match ? match[1] : null;
}

function isChinese(text: string): boolean {
  return /[\u4e00-\u9fff]/.test(text);
}

/**
 * Best-effort localization of free-form backend error messages.
 *
 * Backend errors that carry a machine-readable `error_code` (see
 * `errorCodeFromError`) are mapped by code first. The rest are localized by
 * string heuristics, with the known tradeoff (NOT a bug): raw Chinese
 * messages are passed through verbatim even under the `en` locale, while raw
 * English messages are shown only under `en` and replaced by the fallback key
 * under `zh`.
 */
export function localizeErrorMessage(error: unknown, fallbackKey = 'common.error'): string {
  const candidate = (error && typeof error === 'object' ? error : null) as ErrorLike | null;
  const locale = useI18n.getState().locale;

  const errorCode = errorCodeFromError(error);
  if (errorCode && ERROR_CODE_MESSAGE_KEYS[errorCode]) {
    if (errorCode === 'FILE_CONTENT_MISMATCH') {
      // #232 I-1：结构化 detail 组合本地化文案（format 名走词包）
      return composeFileContentMismatch(errorDetailFromError(error));
    }
    return t(ERROR_CODE_MESSAGE_KEYS[errorCode]);
  }

  const raw =
    toText(candidate?.response?.data?.message) ||
    toText(candidate?.response?.data?.detail) ||
    toText(candidate?.message) ||
    toText(error);

  if (!raw) {
    return t(fallbackKey);
  }

  const lower = raw.toLowerCase();
  const status = extractStatus(raw, candidate ?? undefined);

  if (
    lower.includes('failed to fetch') ||
    lower.includes('network error') ||
    lower.includes('networkerror') ||
    raw.includes('网络连接失败')
  ) {
    return t('common.networkError');
  }

  if (
    lower.includes('request failed with status code') ||
    lower === 'request failed' ||
    raw.includes('请求失败')
  ) {
    if (status) {
      return (
        Number(status) >= 500
          ? t('common.serverErrorWithStatus')
          : t('common.requestFailedWithStatus')
      ).replace('{status}', status);
    }
    return t('common.requestFailed');
  }

  if (lower.includes('download failed') || raw.includes('下载失败')) {
    if (status) {
      return t('common.downloadFailedWithStatus').replace('{status}', status);
    }
    return t('common.downloadFailed');
  }

  if (
    lower.includes('failed to load file') ||
    lower.includes('failed to load config') ||
    lower.startsWith('failed to load ') ||
    lower === 'load failed' ||
    raw.includes('加载文件失败')
  ) {
    if (status) {
      return t('common.loadFailedWithStatus').replace('{status}', status);
    }
    return t('common.loadFailed');
  }

  if (lower === 'fetch failed' || lower === 'export failed' || lower === 'import failed') {
    if (status) {
      return (
        Number(status) >= 500
          ? t('common.serverErrorWithStatus')
          : t('common.requestFailedWithStatus')
      ).replace('{status}', status);
    }
    return t(fallbackKey);
  }

  if (lower === 'failed' || raw === '失败') {
    return t(fallbackKey);
  }

  // Known tradeoff (see docstring): Chinese raw passes through regardless of
  // locale; English raw is swallowed into the fallback under zh.
  if (isChinese(raw)) {
    return raw;
  }

  if (/[A-Za-z]/.test(raw)) {
    return locale === 'en' ? raw : t(fallbackKey);
  }

  return t(fallbackKey);
}
