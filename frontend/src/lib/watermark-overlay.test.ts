// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';
import {
  watermarkOverlayStatusFromVisionQuality,
  watermarkStripChipText,
} from './watermark-overlay';

const t = (key: string) =>
  key === 'batchWizard.step4.pipelineWatermarkStripped' ? '已摘除 {pages} 页' : key;

const hitStatus = {
  ran: true,
  skipped: false,
  failed: false,
  code: 'hit',
  source: 'copy',
  pages_probed: 5,
  pages_hit: 5,
  pages_stripped: 149,
  pages_skipped: 0,
  pages_guard_refused: 0,
  region_count: 0,
};

describe('watermarkOverlayStatusFromVisionQuality', () => {
  it('从任意页的 pipeline_status 里取 watermark_overlay 块', () => {
    const vq = {
      '1': { pipeline_status: { watermark_overlay: hitStatus } },
      '2': { pipeline_status: { watermark_overlay: hitStatus } },
    };
    expect(watermarkOverlayStatusFromVisionQuality(vq)).toEqual(hitStatus);
  });

  it('旧文件/未跑识别/非对象输入一律返回 null', () => {
    expect(watermarkOverlayStatusFromVisionQuality(undefined)).toBeNull();
    expect(watermarkOverlayStatusFromVisionQuality({})).toBeNull();
    expect(watermarkOverlayStatusFromVisionQuality({ '1': { pipeline_status: {} } })).toBeNull();
    expect(watermarkOverlayStatusFromVisionQuality('oops')).toBeNull();
  });
});

describe('watermarkStripChipText', () => {
  it('命中且 source=copy 时报已摘除页数', () => {
    expect(watermarkStripChipText(t, hitStatus)).toBe('已摘除 149 页');
  });

  it('部分页熔断同样报已摘除（并保留留痕）', () => {
    expect(
      watermarkStripChipText(t, { ...hitStatus, code: 'guard_refused_pages', pages_stripped: 3 }),
    ).toBe('已摘除 3 页');
  });

  it('未命中/未跑/关闭/零页返回 null，不谎报', () => {
    expect(watermarkStripChipText(t, { code: 'no_wm_spans', source: 'original' })).toBeNull();
    expect(watermarkStripChipText(t, { code: 'disabled', source: 'original' })).toBeNull();
    expect(watermarkStripChipText(t, { ...hitStatus, pages_stripped: 0 })).toBeNull();
    expect(watermarkStripChipText(t, null)).toBeNull();
  });

  it('source 不是 copy 时返回 null（未真正产出净化副本）', () => {
    expect(watermarkStripChipText(t, { ...hitStatus, source: 'original' })).toBeNull();
  });
});