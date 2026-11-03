// Copyright 2026 LexMask Contributors

import { beforeEach, describe, expect, it } from 'vitest';

import { useI18n, initI18n } from '@/i18n';
import { errorCodeFromError, localizeErrorMessage, messageFromEnvelope } from './localizeError';

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

describe('FILE_CONTENT_MISMATCH localization (#232 I-1)', () => {
  const ZH_WITH_FORMAT =
    '文件内容实际是PDF 文档，与扩展名 .doc 不匹配——文件可能在传输中损坏或被重命名过；请用原始文件重新导出/另存为后再上传';

  beforeEach(() => {
    useI18n.setState({ locale: 'zh' });
  });

  it('composes the zh message from the response envelope detail', () => {
    const error = {
      response: {
        status: 400,
        data: {
          error_code: 'FILE_CONTENT_MISMATCH',
          message: ZH_WITH_FORMAT,
          detail: { detected_format: 'pdf', extension: '.doc' },
        },
      },
    };
    expect(localizeErrorMessage(error)).toBe(ZH_WITH_FORMAT);
  });

  it('composes from the ApiError instance fields (fetchBlob shape)', () => {
    const error = {
      errorCode: 'FILE_CONTENT_MISMATCH',
      detail: { detected_format: 'text', extension: '.doc' },
      message: 'raw zh',
    };
    expect(localizeErrorMessage(error)).toBe(
      '文件内容实际是纯文本，与扩展名 .doc 不匹配——文件可能在传输中损坏或被重命名过；请用原始文件重新导出/另存为后再上传',
    );
  });

  it('falls back to the generic message when detail is missing', () => {
    const error = { errorCode: 'FILE_CONTENT_MISMATCH', message: 'raw zh' };
    expect(localizeErrorMessage(error)).toBe(
      '文件内容与扩展名不匹配——文件可能在传输中损坏或被重命名过；请用原始文件重新导出/另存为后再上传',
    );
  });

  it('composes the English message under en (the I-1 core fix)', async () => {
    useI18n.setState({ locale: 'en' });
    await initI18n();
    try {
      const error = {
        response: {
          status: 400,
          data: {
            error_code: 'FILE_CONTENT_MISMATCH',
            message: ZH_WITH_FORMAT,
            detail: { detected_format: 'pdf', extension: '.doc' },
          },
        },
      };
      expect(localizeErrorMessage(error)).toBe(
        'The file content is actually a PDF document, which does not match the extension .doc. The file may have been corrupted or renamed in transit; re-export or save it in its original format and upload again.',
      );
    } finally {
      useI18n.setState({ locale: 'zh' });
    }
  });

  it('messageFromEnvelope keeps the raw message for unknown codes', () => {
    const data = {
      error_code: 'SOME_NEW_CODE',
      message: '后端具体原因',
      detail: {},
    };
    expect(messageFromEnvelope(data, 'common.uploadFailed')).toBe('后端具体原因');
  });

  it('messageFromEnvelope falls back to the i18n key when no text is present', () => {
    expect(messageFromEnvelope({ error_code: 'X', message: '', detail: {} }, 'playground.uploadFailed')).toBe(
      '文件上传失败。',
    );
  });
});
