// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import {
  PlaygroundEntityPanel,
  type PlaygroundEntityPanelProps,
} from './playground-entity-panel';
import type { Entity } from '../types';

/**
 * Issue#351 P0（复核右栏文案收敛）：
 * - A1 选中计数恰好一处（删「已选 N / M」副文案，Badge 唯一）；
 * - A2 不可用说明恰好一处（两行常驻 notice 删除，原因由卡内 desc 承载）；
 * - A4 识别卡灰字说明合计 ≤1（三段收敛为一行合并提示）；
 * - A7 来源标签同源全隐、混合源仅差异行标注。
 *
 * 冷审修订纪律：「无 X」负向断言必须与「有 Y」正向断言成对，防选择器打空恒绿。
 */

const sameLabelEntities: Entity[] = [
  { id: 'e1', text: '张三', type: 'PERSON', start: 0, end: 2, selected: true, source: 'has' },
  { id: 'e2', text: '李四', type: 'PERSON', start: 10, end: 12, selected: false, source: 'llm' },
];
const mixedSourceEntities: Entity[] = [
  { id: 'e1', text: '张三', type: 'PERSON', start: 0, end: 2, selected: true, source: 'has' },
  { id: 'e2', text: '李四', type: 'PERSON', start: 10, end: 12, selected: false, source: 'regex' },
  { id: 'e3', text: '王五', type: 'PERSON', start: 20, end: 22, selected: false, source: 'has' },
];

// props mock 逐键对齐 playground-entity-panel.simplified.test.tsx 的 baseProps 形状
const baseProps: PlaygroundEntityPanelProps = {
  isImageMode: false,
  replacementLocked: false,
  isLoading: false,
  recognitionIssue: null,
  visualSkippedNotice: null,
  entities: sameLabelEntities,
  mappingEntities: sameLabelEntities,
  entityTypes: [],
  visionTypes: [],
  visibleBoxes: [],
  selectedCount: 1,
  displaySelectedCount: 1,
  displayTotalCount: 2,
  displayStats: { PERSON: { total: 2, selected: 1 } },
  processingMode: 'mask',
  setProcessingMode: vi.fn(),
  maskDisabled: false,
  pseudonymMap: {},
  onPseudonymChange: vi.fn(),
  pseudonymMapLoading: false,
  pseudonymMapError: null,
  onRetryPseudonymLoad: vi.fn(),
  replaceUnready: false,
  pseudonymConflicts: new Set<string>(),
  watermarkText: '',
  setWatermarkText: vi.fn(),
  watermarkDelivery: 'keep',
  setWatermarkDelivery: vi.fn(),
  clearPlaygroundTextPresetTracking: vi.fn(),
  presetQuickSwitch: null,
  onRerunNer: vi.fn(),
  onRedact: vi.fn(),
  onSelectAll: vi.fn(),
  onDeselectAll: vi.fn(),
  onToggleBox: vi.fn(),
  onEntityClick: vi.fn(),
  onRemoveEntity: vi.fn(),
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#351 A1：选中计数恰好一处', () => {
  it('删「已选 N / M」副文案，Badge 计数保留且唯一', () => {
    render(<PlaygroundEntityPanel {...baseProps} />);
    // 负向：旧副文案清零
    expect(screen.queryByText(/已选/)).toBeNull();
    // 正向：Badge 计数存在且恰好一处
    expect(screen.getAllByText('1/2')).toHaveLength(1);
  });

  it('0/0 边界：计数仍恰好一处', () => {
    render(
      <PlaygroundEntityPanel
        {...baseProps}
        entities={[]}
        mappingEntities={[]}
        selectedCount={0}
        displaySelectedCount={0}
        displayTotalCount={0}
        displayStats={{}}
      />,
    );
    expect(screen.queryByText(/已选/)).toBeNull();
    expect(screen.getAllByText('0/0')).toHaveLength(1);
  });
});

describe('Issue#351 A2：不可用说明恰好一处', () => {
  it('扫描件锁定：无整行 notice，替换卡 desc 保留不可用原因', () => {
    render(<PlaygroundEntityPanel {...baseProps} replacementLocked />);
    expect(screen.queryByTestId('playground-processing-mode-scanned-notice')).toBeNull();
    // 正向：不可用原因仍在卡内 desc（信息不整体丢失）
    expect(screen.getByText('扫描件/图片暂不可用')).not.toBeNull();
  });

  it('maskDisabled：无整行 notice，打码卡 desc 保留限制说明', () => {
    render(<PlaygroundEntityPanel {...baseProps} maskDisabled />);
    expect(screen.queryByTestId('playground-processing-mode-mask-notice')).toBeNull();
    // 正向：desc 承载原本 notice 的信息（mode.maskPdfOnly）
    expect(screen.getByText('打码仅支持 PDF 文件')).not.toBeNull();
  });
});

describe('Issue#351 A4：识别卡灰字说明 ≤1', () => {
  it('仅剩一行合并提示，旧两段说明清零', () => {
    render(<PlaygroundEntityPanel {...baseProps} />);
    // 正向：合并提示存在且恰好一处
    expect(screen.getAllByText(/只刷新当前文件/)).toHaveLength(1);
    // 负向：旧 recognitionSectionDesc / reconfigHint 独有片段清零
    expect(screen.queryByText(/修改配置清单或识别项设置后/)).toBeNull();
    expect(screen.queryByText(/重新选择配置清单/)).toBeNull();
  });
});

describe('Issue#351 A7：来源标签同源全隐、混合源仅差异行', () => {
  function openDrawer() {
    fireEvent.click(screen.getByTestId('playground-advanced-toggle'));
  }

  it('同源（语义来源相同）：来源 Badge 全部隐藏，类型与正文仍在', () => {
    render(<PlaygroundEntityPanel {...baseProps} />);
    openDrawer();
    const rows = screen
      .getAllByTestId(/^playground-entity-(?!panel)/)
      .filter((el) => el.getAttribute('data-testid') !== 'playground-entity-panel');
    expect(rows.length).toBe(2);
    for (const row of rows) {
      expect(/语义模型|正则|手动/.test(row.textContent ?? '')).toBe(false);
    }
    // 正向：类型与正文未被连带删除
    expect(screen.getAllByText('张三').length).toBeGreaterThan(0);
  });

  it('混合来源：仅少数派行标注来源（多数派锚）', () => {
    render(
      <PlaygroundEntityPanel
        {...baseProps}
        entities={mixedSourceEntities}
        mappingEntities={mixedSourceEntities}
      />,
    );
    openDrawer();
    const r1 = screen.getByTestId('playground-entity-e1');
    const r2 = screen.getByTestId('playground-entity-e2');
    const r3 = screen.getByTestId('playground-entity-e3');
    // 多数派（语义模型）为锚：少数派（正则）标注，多数派不标注
    expect(/正则/.test(r2.textContent ?? '')).toBe(true);
    expect(/语义模型/.test(r1.textContent ?? '')).toBe(false);
    expect(/语义模型/.test(r3.textContent ?? '')).toBe(false);
    expect(/正则/.test(r1.textContent ?? '')).toBe(false);
    expect(/正则/.test(r3.textContent ?? '')).toBe(false);
  });

  it('门⑥ Minor-4：首行是少数派时仍标注少数派（锚按多数派而非数据序首行）', () => {
    const minorityFirst: Entity[] = [
      { id: 'm1', text: '甲', type: 'PERSON', start: 0, end: 1, selected: true, source: 'regex' },
      { id: 'm2', text: '乙', type: 'PERSON', start: 2, end: 3, selected: false, source: 'has' },
      { id: 'm3', text: '丙', type: 'PERSON', start: 4, end: 5, selected: false, source: 'llm' },
    ];
    render(<PlaygroundEntityPanel {...baseProps} entities={minorityFirst} mappingEntities={minorityFirst} />);
    openDrawer();
    const r1 = screen.getByTestId('playground-entity-m1');
    const r2 = screen.getByTestId('playground-entity-m2');
    const r3 = screen.getByTestId('playground-entity-m3');
    // 多数派=语义模型（2 票）为锚；数据序首行（正则）恰是少数派 ⇒ 必须标它，而非标多数派两行
    expect(/正则/.test(r1.textContent ?? '')).toBe(true);
    expect(/语义模型/.test(r2.textContent ?? '')).toBe(false);
    expect(/语义模型/.test(r3.textContent ?? '')).toBe(false);
  });
});
