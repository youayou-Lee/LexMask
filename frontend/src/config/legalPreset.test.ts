import { describe, expect, it } from 'vitest';
import { LEGAL_TEXT_TYPE_IDS, resolveLegalTextSelection, sameIdSelection } from './legalPreset';

describe('Issue#262：法律卷宗预设', () => {
  it('11 项法律类型清单冻结', () => {
    expect(LEGAL_TEXT_TYPE_IDS).toEqual([
      'PERSON', 'ID_CARD', 'PHONE', 'ADDRESS', 'BIRTH_DATE', 'BANK_CARD',
      'LEGAL_PLAINTIFF', 'LEGAL_DEFENDANT', 'LEGAL_THIRD_PARTY', 'LEGAL_ATTORNEY', 'CASE_NUMBER',
    ]);
  });
  it('与目录交集保序', () => {
    expect(resolveLegalTextSelection(['CASE_NUMBER', 'PERSON', 'MED_PATIENT', 'PHONE']))
      .toEqual(['PERSON', 'PHONE', 'CASE_NUMBER']);
  });
  it('目录漂移交集为空时回落全量（fail-open）', () => {
    expect(resolveLegalTextSelection(['FOO', 'BAR'])).toEqual(['FOO', 'BAR']);
  });
});

describe('Issue#262 终审：sameIdSelection 集合相等', () => {
  it('忽略顺序判等', () => {
    expect(sameIdSelection(['A', 'B'], ['B', 'A'])).toBe(true);
    expect(sameIdSelection([], [])).toBe(true);
  });
  it('重复 id 不与不同 id 判等（[X,X] vs [X,Y] → false，比较去重后长度）', () => {
    expect(sameIdSelection(['A', 'A'], ['A', 'B'])).toBe(false);
    expect(sameIdSelection(['A'], ['A', 'B'])).toBe(false);
    expect(sameIdSelection(['A', 'B', 'B'], ['A', 'B', 'C'])).toBe(false);
  });
  it('重复 id 去重后同集仍判等（[X,X] vs [X] → true）', () => {
    expect(sameIdSelection(['A', 'A'], ['A'])).toBe(true);
    expect(sameIdSelection(['A', 'B', 'B'], ['B', 'A'])).toBe(true);
  });
  it('长度不同或元素不同判不等', () => {
    expect(sameIdSelection(['A', 'B'], ['A'])).toBe(false);
    expect(sameIdSelection(['A', 'B'], ['A', 'C'])).toBe(false);
  });
});
