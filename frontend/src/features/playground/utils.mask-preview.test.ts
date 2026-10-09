// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';
import { getModePreview, maskAliasPreview, pickPreviewSample } from './utils';
import type { Entity } from './types';

function entity(partial: Partial<Entity>): Entity {
  return {
    id: String(partial.id ?? 'e1'),
    text: partial.text ?? '',
    type: partial.type ?? 'PERSON',
    start: 0,
    end: 0,
    selected: partial.selected ?? true,
    source: 'llm',
    page: 1,
    ...partial,
  } as Entity;
}

describe('pickPreviewSample (Issue #79)', () => {
  it('列表第一个是机构时，优先取人名实体做样例', () => {
    const entities = [
      entity({ id: 'a', text: '英德市人民检察院', type: 'INSTITUTION_NAME' }),
      entity({ id: 'b', text: '范科威', type: 'PERSON' }),
    ];
    expect(pickPreviewSample(entities)?.text).toBe('范科威');
  });

  it('当事人角色类型（原告/律师等）视同人名', () => {
    const entities = [
      entity({ id: 'a', text: '某公司', type: 'ORG' }),
      entity({ id: 'b', text: '张三', type: 'LEGAL_ATTORNEY' }),
    ];
    expect(pickPreviewSample(entities)?.text).toBe('张三');
  });

  it('无人名时回退第一个有文本的选中实体', () => {
    const entities = [
      entity({ id: 'a', text: '英德市人民检察院', type: 'INSTITUTION_NAME' }),
      entity({ id: 'b', text: '', type: 'PERSON', selected: false }),
    ];
    expect(pickPreviewSample(entities)?.text).toBe('英德市人民检察院');
  });

  it('跳过未选中实体；空列表返回 undefined', () => {
    const entities = [
      entity({ id: 'a', text: '张三', type: 'PERSON', selected: false }),
      entity({ id: 'b', text: '李四', type: 'PERSON' }),
    ];
    expect(pickPreviewSample(entities)?.text).toBe('李四');
    expect(pickPreviewSample([])).toBeUndefined();
    expect(pickPreviewSample(undefined)).toBeUndefined();
  });
});

describe('getModePreview mask 口径 (Issue #79 v2)', () => {
  it('掩码替换=化名派生：中文姓+某+序号（范某1），非星号', () => {
    const sample = entity({ text: '范科威', type: 'PERSON' });
    expect(getModePreview('mask', sample)).toBe('范科威 -> 范某1');
  });

  it('maskAliasPreview：非 CJK 首字回落后端 derived 兜底基名「某人1」', () => {
    expect(maskAliasPreview('John Smith')).toBe('某人1');
    expect(maskAliasPreview('')).toBe('某人1');
  });

  it('getModePreview 不挑样例：人名优先由 pickPreviewSample 在组件层保证', () => {
    const sample = entity({ text: '范科威', type: 'PERSON' });
    expect(getModePreview('structured', sample)).toBe('范科威 -> <人物[001].个人.姓名>');
    expect(getModePreview('smart', sample)).toBe('范科威 -> [当事人一]');
    expect(getModePreview('structured', entity({ text: '英德市人民检察院', type: 'ORG' }))).toBe(
      '英德市人民检察院 -> <人物[001].个人.姓名>',
    );
  });
});
