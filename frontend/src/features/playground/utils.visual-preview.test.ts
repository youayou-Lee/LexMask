// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';
import {
  boxesForRedactPayload,
  boxesForReplacePreview,
  isVisualPreviewMode,
  mergeNerBoxes,
  syncEntitiesWithNerBoxes,
} from './utils';
import type { BoundingBox } from './types';

// Issue #83：预览范式由文件形态决定，与处理方式无关
describe('isVisualPreviewMode', () => {
  it('扫描件/图片恒为页面范式', () => {
    expect(isVisualPreviewMode('pdf_scanned', true)).toBe(true);
    expect(isVisualPreviewMode('image', false)).toBe(true);
    expect(isVisualPreviewMode('docx', true)).toBe(true);
  });

  it('文本型 PDF：两种处理方式都是页面范式（打码=可编辑框，替换=只读高亮）', () => {
    expect(isVisualPreviewMode('pdf', false)).toBe(true);
  });

  it('docx/txt 等文本格式恒为文本范式（打码被 #59 门控不存在）', () => {
    expect(isVisualPreviewMode('docx', false)).toBe(false);
    expect(isVisualPreviewMode('txt', false)).toBe(false);
  });

  it('无文件类型时为 false', () => {
    expect(isVisualPreviewMode(undefined, false)).toBe(false);
  });
});

// Issue #83：替换页面视图的展示框——只取 ner 定位框，随实体勾选态联动
describe('boxesForReplacePreview', () => {
  const nerBox = (text: string, overrides: Partial<BoundingBox> = {}): BoundingBox =>
    ({ id: `ner_${text}`, source: 'ner', text, type: 'NAME', selected: true, ...overrides }) as never;
  const entityByText = new Map([
    ['张三', { selected: true }],
    ['李四', { selected: false }],
  ]);

  it('只取 ner 框：manual 框不进替换预览（打码工作台对象，执行也不上送）', () => {
    const boxes = [nerBox('张三'), { id: 'm1', source: 'manual', text: '手工框' } as never];
    const out = boxesForReplacePreview(boxes, entityByText);
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe('张三');
  });

  it('实体被删除（text 不在实体表）→ 框消失', () => {
    const boxes = [nerBox('张三'), nerBox('王五')];
    const out = boxesForReplacePreview(boxes, entityByText);
    expect(out.map((b) => b.text)).toEqual(['张三']);
  });

  it('实体取消勾选 → 框呈未选态（与文本视图半透明 mark 同语义）', () => {
    const out = boxesForReplacePreview([nerBox('李四')], entityByText);
    expect(out[0].selected).toBe(false);
  });

  it('实体勾选态缺省（undefined）→ 视为选中（与 mergeNerBoxes 同口径）', () => {
    const out = boxesForReplacePreview([nerBox('赵六')], new Map([['赵六', {}]]));
    expect(out[0].selected).toBe(true);
  });

  it('实体表为空 → 全部框消失', () => {
    expect(boxesForReplacePreview([nerBox('张三')], new Map())).toEqual([]);
  });
});

// Issue #66 A 案：替换模式不携带拉框（防后端误路由图像管线）
describe('boxesForRedactPayload', () => {
  const boxes = [{ id: 'b1' } as never, { id: 'b2' } as never];

  it('文本型文件 + 替换模式：不携带（框不参与替换执行）', () => {
    expect(boxesForRedactPayload(false, 'replace', boxes)).toEqual([]);
  });

  it('文本型 PDF + 打码模式：携带（拉框参与栅格化）', () => {
    expect(boxesForRedactPayload(false, 'mask', boxes)).toEqual(boxes);
  });

  it('扫描件/图片：两种模式都携带（既有行为不变）', () => {
    expect(boxesForRedactPayload(true, 'mask', boxes)).toEqual(boxes);
    expect(boxesForRedactPayload(true, 'replace', boxes)).toEqual(boxes);
  });
});

// Issue #66：识别实体自动成框的合并与执行同步
describe('mergeNerBoxes', () => {
  it('重新定位替换全部 ner 框、保留手拉框', () => {
    const manual = { id: 'm1', source: 'manual', text: '手工框' } as never;
    const oldNer = { id: 'n1', source: 'ner', text: '旧实体' } as never;
    const newNer = [{ id: 'n2', source: 'ner', text: '新实体' } as never];
    const merged = mergeNerBoxes([manual, oldNer], newNer);
    expect(merged.map((b: never) => (b as { id: string }).id)).toEqual(['m1', 'n2']);
  });
});

describe('syncEntitiesWithNerBoxes', () => {
  const entities = [
    { text: '张三', selected: true },
    { text: '李四', selected: true }, // 无框实体（定位失败）
  ];
  it('实体选中跟随其 ner 框；无框实体保持原选中（后端兜底）', () => {
    const boxes = [
      { text: '张三', source: 'ner', selected: false } as never,
    ];
    const synced = syncEntitiesWithNerBoxes(entities, boxes);
    expect(synced[0].selected).toBe(false); // 框被取消勾选 → 实体不选
    expect(synced[1].selected).toBe(true); // 无框 → 保持
  });
  it('manual 框不影响实体同步', () => {
    const boxes = [{ text: '张三', source: 'manual', selected: false } as never];
    const synced = syncEntitiesWithNerBoxes(entities, boxes);
    expect(synced[0].selected).toBe(true); // manual 框不算实体的 UI
  });
});

describe('mergeNerBoxes 勾选继承（增量评审 I3）', () => {
  const located = [{ id: 'ner_new', source: 'ner', text: '张三', selected: true } as never];

  it('实体侧取消勾选 → 新框保持未选（替换模式的选择不被翻转）', () => {
    const entityByText = new Map([['张三', { selected: false }]]);
    const merged = mergeNerBoxes([], located, entityByText);
    expect(merged[0].selected).toBe(false);
  });

  it('旧 ner 框未选（用户在打码模式取消）→ 重定位后保持未选', () => {
    const prev = [{ id: 'ner_old', source: 'ner', text: '张三', selected: false } as never];
    const merged = mergeNerBoxes(prev, located);
    expect(merged[0].selected).toBe(false);
  });

  it('两来源都未明确取消 → 默认选中', () => {
    const merged = mergeNerBoxes([], located, new Map());
    expect(merged[0].selected).toBe(true);
  });
});
