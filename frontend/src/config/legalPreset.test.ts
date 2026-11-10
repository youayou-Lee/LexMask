import { describe, expect, it } from 'vitest';
import { LEGAL_TEXT_TYPE_IDS, resolveLegalTextSelection } from './legalPreset';

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
