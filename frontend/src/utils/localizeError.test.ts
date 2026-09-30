// Copyright 2026 LexMask Contributors

import { beforeEach, describe, expect, it } from 'vitest';

import { useI18n } from '@/i18n';
import { errorCodeFromError, localizeErrorMessage } from './localizeError';

const encryptedError = {
  response: {
    status: 400,
    data: {
      error_code: 'PDF_ENCRYPTED_NEEDS_PASSWORD',
      message: '该 PDF 已加密（受打开密码保护），请输入密码后继续处理',
    },
  },
};

const wrongPasswordError = {
  response: {
    status: 400,
    data: { error_code: 'PDF_WRONG_PASSWORD', message: '密码错误，请重试' },
  },
};

describe('errorCodeFromError', () => {
  it('reads the machine-readable code from the backend error envelope', () => {
    expect(errorCodeFromError(encryptedError)).toBe('PDF_ENCRYPTED_NEEDS_PASSWORD');
    expect(errorCodeFromError(wrongPasswordError)).toBe('PDF_WRONG_PASSWORD');
  });

  it('returns null when the envelope carries no code (legacy errors)', () => {
    expect(errorCodeFromError(new Error('boom'))).toBeNull();
    expect(errorCodeFromError({ response: { status: 404, data: { detail: 'x' } } })).toBeNull();
    expect(errorCodeFromError(null)).toBeNull();
  });
});

describe('localizeErrorMessage error-code priority (#30)', () => {
  beforeEach(() => {
    // en 词包懒加载，单测固定 zh（词包静态内置），en 文案由 i18n parity 测试保障
    useI18n.setState({ locale: 'zh' });
  });

  it('maps known codes before any string heuristics', () => {
    expect(localizeErrorMessage(encryptedError)).toBe('该 PDF 已加密，需要输入密码才能处理。');
    expect(localizeErrorMessage(wrongPasswordError)).toBe('密码错误，请重试。');
  });

  it('keeps legacy no-code errors on the string-heuristics path', () => {
    // 无码英文原文在 zh 下仍落通用兜底（既有约定，不因本次改动变化）
    expect(localizeErrorMessage(new Error('document closed or encrypted'))).toBe('操作失败');
  });
});
