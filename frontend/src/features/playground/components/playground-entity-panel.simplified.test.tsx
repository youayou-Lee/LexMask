// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import {
  PlaygroundEntityPanel,
  type PlaygroundEntityPanelProps,
} from './playground-entity-panel';
import type { Entity } from '../types';

/**
 * Issue#262 A2（Task 7）：复核态右栏精简——主控件 ≤5 + 底部「高级」抽屉收纳细粒度操作。
 *
 * 钉住三条不变量：
 * 1) 复核态主控件齐全（①统计摘要+全选/清空 ②处理方式 ⑤执行按钮 + 识别区），
 *    逐类型统计/逐实体列表/水印文本输入默认不可见（收进高级抽屉，默认收起）；
 * 2) 展开抽屉后列表与类型统计可达——移动不删码，testid 原样；
 * 3) 显隐条件逐字保留：WatermarkDeliverySelector 仅 mask、
 *    PseudonymMapSection 仅 !replacementLocked && mode==='replace'，且都留在主控件区。
 */

const mockEntities: Entity[] = [
  { id: 'e1', text: '张三', type: 'PERSON', start: 0, end: 2, selected: true, source: 'has' },
  { id: 'e2', text: '李四', type: 'PERSON', start: 10, end: 12, selected: false, source: 'llm' },
];

// props mock 逐键对齐 playground-page.tsx:491-550 的传参形状（值取最简可渲染形态）
const baseProps: PlaygroundEntityPanelProps = {
  isImageMode: false,
  replacementLocked: false,
  isLoading: false,
  recognitionIssue: null,
  visualSkippedNotice: null,
  entities: mockEntities,
  mappingEntities: mockEntities,
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

// 实体行 testid 是 playground-entity-{id}，与根节点 playground-entity-panel 共享前缀；
// testing-library 对 regex 取子串匹配，必须用负向前瞻把根节点排除，只匹配列表行。
const ENTITY_ROW = /^playground-entity-(?!panel)/;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#262：复核态右栏精简', () => {
  it('主控件齐全且默认收起高级区（列表/类型统计/水印文本输入不可见）', () => {
    render(<PlaygroundEntityPanel {...baseProps} />);
    // ① 统计摘要+全选/清空
    expect(screen.getByTestId('playground-select-all')).not.toBeNull();
    expect(screen.getByTestId('playground-deselect-all')).not.toBeNull();
    // ② 处理方式
    expect(screen.getByTestId('playground-processing-mode-mask')).not.toBeNull();
    // ⑤ 执行按钮（主 CTA 不进抽屉）
    expect(screen.getByTestId('playground-redact-btn')).not.toBeNull();
    // 识别区保留（重识别按钮；presetQuickSwitch 插槽由页面层注入）
    expect(screen.getByTestId('playground-rerun-btn')).not.toBeNull();
    // 高级抽屉开关存在且默认收起（无障碍：aria-expanded）
    const toggle = screen.getByTestId('playground-advanced-toggle');
    expect(toggle).not.toBeNull();
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    // 列表行在抽屉外不可见
    expect(screen.queryAllByTestId(ENTITY_ROW)).toHaveLength(0);
    // 水印文本输入随迁入抽屉：默认不可见
    expect(screen.queryByTestId('playground-watermark-input')).toBeNull();
    // 逐类型统计块在抽屉外不可见（PII 分组标题只出现在抽屉内容区）
    expect(screen.queryAllByText('PII')).toHaveLength(0);
  });

  it('展开高级抽屉后列表与类型统计可达', () => {
    render(<PlaygroundEntityPanel {...baseProps} />);
    fireEvent.click(screen.getByTestId('playground-advanced-toggle'));
    // aria-expanded 翻转
    expect(
      screen.getByTestId('playground-advanced-toggle').getAttribute('aria-expanded'),
    ).toBe('true');
    // 逐实体列表可达（移动不删码：testid 原样）
    expect(screen.getAllByTestId(ENTITY_ROW).length).toBeGreaterThan(0);
    // 逐类型统计块可达
    expect(screen.getAllByText('PII').length).toBeGreaterThan(0);
    // 水印文本输入随迁可达
    expect(screen.getByTestId('playground-watermark-input')).not.toBeNull();
    // 展开不影响主控件：执行按钮仍在
    expect(screen.getByTestId('playground-redact-btn')).not.toBeNull();
  });

  it('replace 模式渲染化名映射、mask 模式渲染交付水印（显隐条件不变，均在主控件区）', () => {
    // mask（baseProps）：交付水印在、化名映射不在
    render(<PlaygroundEntityPanel {...baseProps} />);
    expect(screen.getByTestId('playground-watermark-delivery-keep')).not.toBeNull();
    expect(screen.getByTestId('playground-watermark-delivery-strip')).not.toBeNull();
    expect(screen.queryByTestId('playground-pseudonym-map')).toBeNull();

    cleanup();

    // replace：化名映射在主控件区（无需展开抽屉）、交付水印隐藏
    render(<PlaygroundEntityPanel {...baseProps} processingMode="replace" />);
    expect(screen.getByTestId('playground-pseudonym-map')).not.toBeNull();
    expect(screen.queryByTestId('playground-watermark-delivery-keep')).toBeNull();
    expect(screen.queryByTestId('playground-watermark-delivery-strip')).toBeNull();
  });
});
