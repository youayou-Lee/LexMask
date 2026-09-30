// Copyright 2026 LexMask Contributors

import { t, useI18n } from '@/i18n';

type ErrorLike = {
  message?: unknown;
  status?: unknown;
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
 */
export function errorCodeFromError(error: unknown): string | null {
  const candidate = (error && typeof error === 'object' ? error : null) as ErrorLike | null;
  const code = candidate?.response?.data?.error_code;
  return typeof code === 'string' && code ? code : null;
}

const ERROR_CODE_MESSAGE_KEYS: Record<string, string> = {
  PDF_ENCRYPTED_NEEDS_PASSWORD: 'common.pdfEncrypted',
  PDF_WRONG_PASSWORD: 'common.pdfWrongPassword',
};

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
