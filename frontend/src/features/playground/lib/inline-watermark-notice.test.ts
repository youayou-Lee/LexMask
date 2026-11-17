import { describe, expect, it, vi } from 'vitest';

import { inlineWatermarkNoticeText } from './inline-watermark-notice';

/**
 * Issue#338 T11（脏数据口径）：只有有限正整数才出提示。
 *
 * 变异自证：把 `inline-watermark-notice.ts` 的守卫改成只判 null/undefined，
 * 本文件中「0 / 负数 / 非整数 / NaN / Infinity / 字符串 / 布尔 / 对象」用例必红。
 */
const TRANSLATE = (key: string) => {
  if (key === 'playground.inlineWatermarkNotice') {
    return '已从文档结构层剔除 {count} 行平铺水印文本（正文内容不受影响）';
  }
  return key;
};

describe('Issue#338 内嵌水印提示派生（inlineWatermarkNoticeText）', () => {
  it('正整数 ⇒ 文案带行数', () => {
    expect(inlineWatermarkNoticeText(3, TRANSLATE)).toBe(
      '已从文档结构层剔除 3 行平铺水印文本（正文内容不受影响）',
    );
    expect(inlineWatermarkNoticeText(150, TRANSLATE)).toContain('150');
  });

  it.each([0, -1, -0, 1.5, Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY])(
    '数值 %s ⇒ null（不渲染）',
    (value) => {
      expect(inlineWatermarkNoticeText(value, TRANSLATE)).toBeNull();
    },
  );

  it.each(['3', '', 'abc', true, false, null, undefined, {}, []])(
    '非整数/非数值 %s ⇒ null（不渲染）',
    (value) => {
      expect(inlineWatermarkNoticeText(value, TRANSLATE)).toBeNull();
    },
  );

  it('脏数据不触发 i18n 查表（translate 零调用）', () => {
    const translate = vi.fn(TRANSLATE);
    inlineWatermarkNoticeText('3', translate);
    inlineWatermarkNoticeText(0, translate);
    inlineWatermarkNoticeText(Number.NaN, translate);
    expect(translate).not.toHaveBeenCalled();
  });

  it('合法值只查一次表并替换 {count}', () => {
    const translate = vi.fn(TRANSLATE);
    expect(inlineWatermarkNoticeText(7, translate)).toContain('7');
    expect(translate).toHaveBeenCalledTimes(1);
    expect(translate).toHaveBeenCalledWith('playground.inlineWatermarkNotice');
  });
});