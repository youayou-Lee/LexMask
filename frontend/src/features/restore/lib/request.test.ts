import { describe, expect, it } from 'vitest';
import { buildRestoreRequest, normalizePolicy } from './request';

describe('normalizePolicy——policy 档位归一', () => {
  it('undefined/缺省 → safe（与后端默认一致）', () => {
    expect(normalizePolicy(undefined)).toBe('safe');
  });

  it('"safe" → safe', () => {
    expect(normalizePolicy('safe')).toBe('safe');
  });

  it('"first" → first', () => {
    expect(normalizePolicy('first')).toBe('first');
  });

  it('任意脏值 → safe 兜底（后端对非法 policy 会 400，前端不外发）', () => {
    expect(normalizePolicy('FIRST')).toBe('safe');
    expect(normalizePolicy(42)).toBe('safe');
    expect(normalizePolicy(null)).toBe('safe');
  });
});

describe('buildRestoreRequest——请求体构造', () => {
  const mapping = { '[NAME_1]': { type: 'person', texts: ['张三'] } };

  it('三字段齐全，policy 落到请求体', () => {
    expect(buildRestoreRequest('含[NAME_1]的文本', mapping, 'first')).toEqual({
      text: '含[NAME_1]的文本',
      mapping,
      policy: 'first',
    });
  });

  it('默认档 safe', () => {
    expect(buildRestoreRequest('text', mapping, undefined).policy).toBe('safe');
  });
});
