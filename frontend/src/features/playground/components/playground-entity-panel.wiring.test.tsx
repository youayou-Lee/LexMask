// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import {
  PlaygroundEntityPanel,
  type PlaygroundEntityPanelProps,
} from './playground-entity-panel';
import type { BoundingBox, Entity } from '../types';

/**
 * Issue#270：复核面板回调接线钉（issue 原文「复核面板回调接线断言（onRedact 等未钉调用）」）。
 *
 * 覆盖面：面板内部 prop→DOM 接线——onClick 挂丢/触发语义错（tsc 抓不住：类型吻合、语义错）。
 * 页面层 playground-page.tsx 向面板传参的错误不在覆盖面（页面层无测试，见方案 §1.5）。
 * simplified.test.tsx 钉存在性与显隐形态；本文件钉「点了真的会调、实参形态正确」。
 *
 * 实参规格（冷评审 F1 修正）：四个主控件在组件内均为 onClick={onRedact} 等直传，
 * React 运行时把 SyntheticEvent 作为首参传入（props 的 () => void 只是类型面宽松），
 * 因此断言「恰好一次」；实参钉用 expect.anything()——不得写零参断言（对现状必红）。
 * onEntityClick(entity, event) / onToggleBox(id)（箭头包裹）/ onRemoveEntity(id) 实参形态
 * 已逐点核实。radio 类控件 testid 在 label 上、input 为 sr-only，jsdom 对 label 激活
 * 转发不可靠——一律 within(label).getByRole('radio') 后 fire（冷评审 F6）。
 */

const mockEntities: Entity[] = [
  { id: 'e1', text: '张三', type: 'PERSON', start: 0, end: 2, selected: true, source: 'has' },
  { id: 'e2', text: '李四', type: 'PERSON', start: 10, end: 12, selected: false, source: 'llm' },
];

const mockBoxes: BoundingBox[] = [
  {
    id: 'b1',
    x: 10,
    y: 10,
    width: 20,
    height: 20,
    type: 'official_seal',
    text: '公章',
    selected: false,
    source: 'visual_features',
  },
];

const makeProps = (overrides: Partial<PlaygroundEntityPanelProps> = {}): PlaygroundEntityPanelProps => ({
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
  ...overrides,
});

// 实体行 testid 是 playground-entity-{id}，与根节点 playground-entity-panel 共享前缀；
// testing-library 对 regex 取子串匹配，必须用负向前瞻把根节点排除（同 simplified 约定）。
const ENTITY_ROW = /^playground-entity-(?!panel)/;

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#270：复核面板回调接线钉', () => {
  it('用例1+7 主控件四回调：redact/rerun/select-all/deselect-all → 恰好一次 + 实参 expect.anything()（直传接线，首参为 click 事件，不得零参断言）', () => {
    const props = makeProps();
    render(<PlaygroundEntityPanel {...props} />);

    fireEvent.click(screen.getByTestId('playground-redact-btn'));
    fireEvent.click(screen.getByTestId('playground-rerun-btn'));
    fireEvent.click(screen.getByTestId('playground-select-all'));
    fireEvent.click(screen.getByTestId('playground-deselect-all'));

    // 恰好一次：防事件冒泡双触发回归（F1 防御意图并入本用例）
    expect(props.onRedact).toHaveBeenCalledTimes(1);
    expect(props.onRerunNer).toHaveBeenCalledTimes(1);
    expect(props.onSelectAll).toHaveBeenCalledTimes(1);
    expect(props.onDeselectAll).toHaveBeenCalledTimes(1);
    // 直传接线会收到 SyntheticEvent 首参——钉「有实参形态」而非「无参」
    expect(props.onRedact).toHaveBeenCalledWith(expect.anything());
    expect(props.onRerunNer).toHaveBeenCalledWith(expect.anything());
    expect(props.onSelectAll).toHaveBeenCalledWith(expect.anything());
    expect(props.onDeselectAll).toHaveBeenCalledWith(expect.anything());
  });

  it('用例2 实体行：点击行 → onEntityClick(entity, event)；行内移除按钮 → onRemoveEntity(id)', () => {
    const props = makeProps();
    render(<PlaygroundEntityPanel {...props} />);

    // 实体列表在「高级」抽屉内（Issue#262 A2 收纳）——先展开
    fireEvent.click(screen.getByTestId('playground-advanced-toggle'));
    const rows = screen.getAllByTestId(ENTITY_ROW);
    expect(rows.length).toBe(2);
    fireEvent.click(rows[0]);
    expect(props.onEntityClick).toHaveBeenCalledTimes(1);
    expect(props.onEntityClick).toHaveBeenCalledWith(mockEntities[0], expect.anything());

    // 移除按钮无 testid，以 aria-label 定位；stopPropagation 保证不触发行点击
    const removeButtons = screen.getAllByLabelText('移除标注');
    expect(removeButtons).toHaveLength(2);
    const entityClicksBeforeRemove = vi.mocked(props.onEntityClick).mock.calls.length;
    fireEvent.click(removeButtons[0]);
    expect(props.onRemoveEntity).toHaveBeenCalledTimes(1);
    expect(props.onRemoveEntity).toHaveBeenCalledWith('e1');
    // stopPropagation：移除点击不冒泡成行点击（行点击数不增加）
    expect(props.onEntityClick).toHaveBeenCalledTimes(entityClicksBeforeRemove);
  });

  it('用例3 图像模式框选：点击 playground-box-{id} → onToggleBox(id)', () => {
    const props = makeProps({ isImageMode: true, visibleBoxes: mockBoxes, selectedCount: 0 });
    render(<PlaygroundEntityPanel {...props} />);

    // 框列表同样在「高级」抽屉内——先展开
    fireEvent.click(screen.getByTestId('playground-advanced-toggle'));
    fireEvent.click(screen.getByTestId('playground-box-b1'));
    expect(props.onToggleBox).toHaveBeenCalledTimes(1);
    expect(props.onToggleBox).toHaveBeenCalledWith('b1');
  });

  it('用例4 高级抽屉：watermark-input 输入 → setWatermarkText(text)；delivery-strip / mode-replace radio（label 内 input）→ 对应 setter', () => {
    const props = makeProps();
    render(<PlaygroundEntityPanel {...props} />);

    // 高级抽屉默认收起：先展开
    fireEvent.click(screen.getByTestId('playground-advanced-toggle'));
    const watermarkInput = screen.getByTestId('playground-watermark-input');
    fireEvent.change(watermarkInput, { target: { value: '机密水印' } });
    expect(props.setWatermarkText).toHaveBeenCalledWith('机密水印');

    // F6：testid 在 label 上、input sr-only——对 label 内 radio fire
    const stripRadio = within(screen.getByTestId('playground-watermark-delivery-strip')).getByRole(
      'radio',
    );
    fireEvent.click(stripRadio);
    expect(props.setWatermarkDelivery).toHaveBeenCalledWith('strip');

    const replaceRadio = within(screen.getByTestId('playground-processing-mode-replace')).getByRole(
      'radio',
    );
    fireEvent.click(replaceRadio);
    expect(props.setProcessingMode).toHaveBeenCalledWith('replace');
  });

  it('用例5 replace 模式化名映射：pseudonym-input-{text} 输入 → onPseudonymChange(text, value)；error 态 retry → onRetryPseudonymLoad', () => {
    const props = makeProps({
      processingMode: 'replace',
      pseudonymMap: { 张三: '' },
      pseudonymMapError: '化名表加载失败',
    });
    render(<PlaygroundEntityPanel {...props} />);

    const input = screen.getByTestId('pseudonym-input-张三');
    fireEvent.change(input, { target: { value: '李别名' } });
    expect(props.onPseudonymChange).toHaveBeenCalledTimes(1);
    expect(props.onPseudonymChange).toHaveBeenCalledWith('张三', '李别名');

    fireEvent.click(screen.getByTestId('playground-pseudonym-retry'));
    expect(props.onRetryPseudonymLoad).toHaveBeenCalledTimes(1);
  });

  it('用例6a 空态变体：totalCount=0 → playground-disabled-rerun → onRerunNer（onSelectAll 不调）', () => {
    // totalCount = displayTotalCount ?? entities.length——必须显式置 0（守卫按 totalCount 判空）
    const props = makeProps({
      entities: [],
      mappingEntities: [],
      displayTotalCount: 0,
      displaySelectedCount: 0,
      recognitionIssue: '识别服务不可用',
    });
    render(<PlaygroundEntityPanel {...props} />);

    const rerunBtn = screen.getByTestId('playground-disabled-rerun');
    fireEvent.click(rerunBtn);
    expect(props.onRerunNer).toHaveBeenCalledTimes(1);
    expect(props.onSelectAll).not.toHaveBeenCalled();
    expect(screen.queryByTestId('playground-disabled-select-all')).toBeNull();
  });

  it('用例6b 非空被挡变体：totalCount>0 且 selected=0 → playground-disabled-select-all → onSelectAll（onRerunNer 不调）', () => {
    const props = makeProps({ displaySelectedCount: 0 });
    render(<PlaygroundEntityPanel {...props} />);

    const selectAllBtn = screen.getByTestId('playground-disabled-select-all');
    fireEvent.click(selectAllBtn);
    expect(props.onSelectAll).toHaveBeenCalledTimes(1);
    expect(props.onRerunNer).not.toHaveBeenCalled();
    expect(screen.queryByTestId('playground-disabled-rerun')).toBeNull();
  });
});
