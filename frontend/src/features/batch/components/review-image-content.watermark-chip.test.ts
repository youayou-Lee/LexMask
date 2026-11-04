import { describe, expect, it } from 'vitest';

import { watermarkStripChipText } from './review-image-content';

const t = ((key: string) => key) as unknown as Parameters<typeof watermarkStripChipText>[0];

describe('watermarkStripChipText（Issue#236 复核页可见性）', () => {
  it('命中且走净化副本时给出「已摘除 N 页」', () => {
    expect(
      watermarkStripChipText(t, { code: 'hit', source: 'copy', pages_stripped: 149 }),
    ).toBe('batchWizard.step4.pipelineWatermarkStripped');
  });

  it('同一标签在部分页残留熔断时同样报页数', () => {
    expect(
      watermarkStripChipText(t, { code: 'guard_refused_pages', source: 'copy', pages_stripped: 3 }),
    ).toBe('batchWizard.step4.pipelineWatermarkStripped');
  });

  it('未命中 / 未跑 / 关闭时回落通用状态标签（不谎报摘除）', () => {
    expect(watermarkStripChipText(t, { code: 'no_wm_spans', source: 'original' })).toBeNull();
    expect(watermarkStripChipText(t, { code: 'not_run', source: 'original' })).toBeNull();
    expect(watermarkStripChipText(t, { code: 'disabled' })).toBeNull();
  });

  it('source 不是 copy 时不显示摘除（防止状态面自相矛盾）', () => {
    expect(watermarkStripChipText(t, { code: 'hit', source: 'original', pages_stripped: 3 })).toBeNull();
  });
});
