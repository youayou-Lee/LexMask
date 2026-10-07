import { describe, expect, it } from 'vitest';
import { exceedsSizeLimit, parseMappingJson } from './mapping-guard';

describe('parseMappingJson——映射表前端预检', () => {
  it('合法新格式 {type,texts[]} → ok 且原样透传', () => {
    const raw = JSON.stringify({
      '[NAME_1]': { type: 'person', texts: ['张三', '张四'] },
    });
    const result = parseMappingJson(raw);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.mapping['[NAME_1]']).toEqual({ type: 'person', texts: ['张三', '张四'] });
    }
  });

  it('JSON 语法错 → invalidJson，带解析器原因', () => {
    const result = parseMappingJson('{"[NAME_1]": ');
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toBe('invalidJson');
      expect(result.detail).toBeTruthy();
    }
  });

  it('顶层数组 → notObject（后端 mapping: dict 会 422，前端拦）', () => {
    const result = parseMappingJson('[1,2,3]');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toBe('notObject');
  });

  it('顶层字符串 → notObject', () => {
    const result = parseMappingJson('"映射表.json 的内容误是纯文本"');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toBe('notObject');
  });

  it('顶层 null → notObject（typeof null === "object" 陷阱）', () => {
    const result = parseMappingJson('null');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toBe('notObject');
  });

  it('空对象 → emptyMapping（后端虽合法但结果必全 unknown，前端拦下提示）', () => {
    const result = parseMappingJson('{}');
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.reason).toBe('emptyMapping');
  });

  it('条目级非法（texts 空数组）→ ok 放行给后端，由 parse_warnings 展示', () => {
    const raw = JSON.stringify({
      '[NAME_1]': { texts: [] },
      '[PERSON_1]': { texts: ['张三'] },
    });
    const result = parseMappingJson(raw);
    expect(result.ok).toBe(true);
  });
});

describe('exceedsSizeLimit——大文本护栏', () => {
  it('小文本不超限', () => {
    expect(exceedsSizeLimit('短文本', 1024)).toBe(false);
  });

  it('超限（按 UTF-8 字节算，非字符数）', () => {
    // '中' = 3 字节；4 个中文字符 = 12 字节 > 10 字节上限
    expect(exceedsSizeLimit('中中中中', 10)).toBe(true);
    // 3 个中文字符 = 9 字节 ≤ 10 字节
    expect(exceedsSizeLimit('中中中', 10)).toBe(false);
  });

  it('边界值：恰好等于上限不算超（> 严格大于）', () => {
    expect(exceedsSizeLimit('abc', 3)).toBe(false);
    expect(exceedsSizeLimit('abcd', 3)).toBe(true);
  });

  it('空字符串不超限', () => {
    expect(exceedsSizeLimit('', 0)).toBe(false);
  });
});
