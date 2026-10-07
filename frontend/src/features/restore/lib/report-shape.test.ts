import { describe, expect, it } from 'vitest';
import { buildAmbiguousRows, countOccurrences, formatUnknownList } from './report-shape';
import type { RestoreAmbiguousItem } from './report-shape';

describe('countOccurrences——次数列数据源（方案 v2：前端自算）', () => {
  it('占位符出现两次计 2', () => {
    expect(countOccurrences('[NAME_1]与[NAME_1]约定', '[NAME_1]')).toBe(2);
  });

  it('不出现计 0', () => {
    expect(countOccurrences('没有任何占位符', '[NAME_1]')).toBe(0);
  });

  it('空 key 返回 0（防死循环/全串匹配）', () => {
    expect(countOccurrences('任意文本', '')).toBe(0);
  });

  it('普通字符串按字面计数', () => {
    expect(countOccurrences('张三和张三丰', '张三')).toBe(2);
  });
});

describe('buildAmbiguousRows——ambiguous 表格整形', () => {
  const inputText = '被告[NAME_1]与[NAME_1]的代理人到庭。';

  it('附上输入文本中的出现次数', () => {
    const items: RestoreAmbiguousItem[] = [{ key: '[NAME_1]', candidates: ['张三', '李四'] }];
    const rows = buildAmbiguousRows(inputText, items);
    expect(rows).toEqual([
      { key: '[NAME_1]', candidates: ['张三', '李四'], occurrences: 2, reason: null },
    ]);
  });

  it('忽略服务端 occurrences（HTTP 响应里没有该字段，凡出现皆为脏数据）', () => {
    const items = [
      { key: '[NAME_1]', candidates: ['张三'], occurrences: 99 },
    ] as unknown as RestoreAmbiguousItem[];
    const rows = buildAmbiguousRows(inputText, items);
    expect(rows[0].occurrences).toBe(2);
  });

  it('保留 reason 供 UI 展示', () => {
    const items: RestoreAmbiguousItem[] = [
      { key: '2023年', candidates: ['2023年3月', '2023年5月'], reason: '日期后界不完整' },
    ];
    const rows = buildAmbiguousRows(inputText, items);
    expect(rows[0].reason).toBe('日期后界不完整');
  });

  it('空 ambiguous 返回空数组', () => {
    expect(buildAmbiguousRows(inputText, [])).toEqual([]);
  });
});

describe('formatUnknownList——unknown 警示文案整形', () => {
  it('不超过上限时全展示、无溢出', () => {
    const result = formatUnknownList(['[NAME_9]'], 8);
    expect(result).toEqual({ shown: ['[NAME_9]'], overflow: 0 });
  });

  it('超上限截断并计溢出数', () => {
    const tokens = Array.from({ length: 10 }, (_, i) => `[T_${i}]`);
    const result = formatUnknownList(tokens, 8);
    expect(result.shown).toHaveLength(8);
    expect(result.overflow).toBe(2);
  });

  it('空列表', () => {
    expect(formatUnknownList([], 8)).toEqual({ shown: [], overflow: 0 });
  });
});
