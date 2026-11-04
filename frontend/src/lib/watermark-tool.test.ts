// Copyright 2026 LexMask Contributors

// Issue#244：水印工具纯函数层测试。参照 watermark-overlay.test.ts 的 node 环境写法。

import { describe, expect, it } from 'vitest';

import {
  filenameFromContentDisposition,
  isPdfFamilyFileType,
  isWatermarkStatusCode,
  stripSummaryFields,
  watermarkCodeTextKey,
  watermarkUploadToastPlan,
} from './watermark-tool';

describe('watermarkCodeTextKey', () => {
  it('十个机读 code 各有独立键', () => {
    const codes = [
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
    const keys = codes.map((code) => watermarkCodeTextKey(code));
    for (const key of keys) expect(key.startsWith('watermarkTool.code.')).toBe(true);
    expect(new Set(keys).size).toBe(codes.length);
  });

  it('未知/缺失 code 兜底 unknown 键，不抛错不猜', () => {
    expect(watermarkCodeTextKey('something_new')).toBe('watermarkTool.code.unknown');
    expect(watermarkCodeTextKey(undefined)).toBe('watermarkTool.code.unknown');
    expect(watermarkCodeTextKey(null)).toBe('watermarkTool.code.unknown');
    expect(watermarkCodeTextKey(42)).toBe('watermarkTool.code.unknown');
  });

  it('isWatermarkStatusCode 只认合法枚举', () => {
    expect(isWatermarkStatusCode('hit')).toBe(true);
    expect(isWatermarkStatusCode('HIT')).toBe(false);
    expect(isWatermarkStatusCode('')).toBe(false);
    expect(isWatermarkStatusCode('copy_dir_unwritable')).toBe(false);
  });
});

describe('watermarkUploadToastPlan（上传弹窗三态）', () => {
  it('code=hit → hit 态 + pages_hit 快照', () => {
    const plan = watermarkUploadToastPlan({ code: 'hit', pages_hit: 12 });
    expect(plan).toEqual({
      notice: 'hit',
      messageKey: 'watermarkTool.uploadHitToast',
      pages: 12,
    });
  });

  it('code=hit 且 pages_hit 非正数 → hit 态但 pages=null（不渲染 0 页）', () => {
    expect(watermarkUploadToastPlan({ code: 'hit', pages_hit: 0 }).pages).toBeNull();
    expect(watermarkUploadToastPlan({ code: 'hit' }).pages).toBeNull();
    expect(watermarkUploadToastPlan({ code: 'hit', pages_hit: '3' }).pages).toBeNull();
  });

  it('no_isolated_overlay / below_min_pages → unstrip 警示态', () => {
    for (const code of ['no_isolated_overlay', 'below_min_pages']) {
      const plan = watermarkUploadToastPlan({ code });
      expect(plan.notice).toBe('unstrip');
      expect(plan.messageKey).toBe('watermarkTool.uploadUnstripToast');
      expect(plan.pages).toBeNull();
    }
  });

  it('其余 code 一律静默（null）', () => {
    for (const code of [
      'no_wm_spans',
      'encrypted',
      'capped_by_size',
      'not_pdf',
      'not_applicable',
      'disabled',
      'error',
    ]) {
      expect(watermarkUploadToastPlan({ code })).toEqual({
        notice: null,
        messageKey: null,
        pages: null,
      });
    }
  });

  it('结构不合法（请求失败静默的前提）→ null', () => {
    expect(watermarkUploadToastPlan(null)).toEqual({ notice: null, messageKey: null, pages: null });
    expect(watermarkUploadToastPlan(undefined)).toEqual({
      notice: null,
      messageKey: null,
      pages: null,
    });
    expect(watermarkUploadToastPlan({})).toEqual({ notice: null, messageKey: null, pages: null });
    expect(watermarkUploadToastPlan('hit')).toEqual({ notice: null, messageKey: null, pages: null });
  });
});

describe('isPdfFamilyFileType', () => {
  it('pdf / pdf_scanned 命中，其余（image/docx/缺失）不命中', () => {
    expect(isPdfFamilyFileType('pdf')).toBe(true);
    expect(isPdfFamilyFileType('pdf_scanned')).toBe(true);
    expect(isPdfFamilyFileType('image')).toBe(false);
    expect(isPdfFamilyFileType('docx')).toBe(false);
    expect(isPdfFamilyFileType(undefined)).toBe(false);
  });
});

describe('filenameFromContentDisposition', () => {
  it('优先 RFC 5987 filename*（UTF-8 百分号解码）', () => {
    const header = "attachment; filename*=UTF-8''%E5%8D%B7%E4%B8%80-%E6%97%A0%E6%B0%B4%E5%8D%B0.pdf";
    expect(filenameFromContentDisposition(header)).toBe('卷一-无水印.pdf');
  });

  it('回退带引号 filename=', () => {
    expect(filenameFromContentDisposition('attachment; filename="clean.pdf"')).toBe('clean.pdf');
  });

  it('回退不带引号 filename=', () => {
    expect(filenameFromContentDisposition('attachment; filename=clean.pdf')).toBe('clean.pdf');
  });

  it('非法百分号编码 / 缺失头 / 空 filename → null', () => {
    expect(filenameFromContentDisposition("attachment; filename*=UTF-8''%ZZ.pdf")).toBeNull();
    expect(filenameFromContentDisposition(null)).toBeNull();
    expect(filenameFromContentDisposition('')).toBeNull();
    expect(filenameFromContentDisposition('attachment; filename=""')).toBeNull();
  });
});

describe('stripSummaryFields', () => {
  it('守卫读取数值字段 + clean_available', () => {
    const fields = stripSummaryFields({
      code: 'hit',
      pages_hit: 7,
      pages_stripped: 7,
      pages_skipped: 1,
      pages_guard_refused: 0,
      duration_ms: 1234,
      clean_available: true,
    });
    expect(fields).toEqual({
      pagesHit: 7,
      pagesStripped: 7,
      pagesSkipped: 1,
      guardRefused: 0,
      durationMs: 1234,
      cleanAvailable: true,
    });
  });

  it('缺字段显示 null（不谎报 0），clean_available 非真即假', () => {
    const fields = stripSummaryFields({ code: 'error', clean_available: 'no' });
    expect(fields).toEqual({
      pagesHit: null,
      pagesStripped: null,
      pagesSkipped: null,
      guardRefused: null,
      durationMs: null,
      cleanAvailable: false,
    });
    expect(stripSummaryFields(null).cleanAvailable).toBe(false);
  });
});
