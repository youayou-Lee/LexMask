// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#327 门⑥ C-1 返工：「先匿名化已处理的 N/M 页」入口在识别在飞时必须可点。
//
// 缺陷实证（评审 C-1）：识别在飞时 isLoading 恒 true，而主按钮的 disabled 是
// `shownSelectedCount === 0 || isLoading || replaceUnready`——识别中画布本来就没有框
// （框在审阅提交时才落 file_store），三条全中 ⇒ 子集入口在目标场景里根本不存在。
//
// 本文件钉的是**面板内部判定**：子集口径下按钮必须 enabled（哪怕 isLoading=true、
// 勾选数=0），文案是「先匿名化已处理的 54/86 页」，点击真正回调 onRedact。

import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import {
  PlaygroundEntityPanel,
  type PlaygroundEntityPanelProps,
} from './playground-entity-panel';

const baseProps: PlaygroundEntityPanelProps = {
  isImageMode: true,
  replacementLocked: true,
  isLoading: true,
  recognitionIssue: null,
  visualSkippedNotice: null,
  entities: [],
  mappingEntities: [],
  entityTypes: [],
  visionTypes: [],
  visibleBoxes: [],
  selectedCount: 0,
  displaySelectedCount: 0,
  displayTotalCount: 0,
  processingMode: 'mask',
  setProcessingMode: vi.fn(),
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

describe('Issue#327：识别在飞时的子集入口（门⑥ C-1）', () => {
  it('子集口径下主按钮可点：isLoading=true + 0 勾选也不 disabled，文案带 N/M', () => {
    const props = { ...baseProps, subsetReadyCount: 54, subsetTotalPages: 86 };
    render(<PlaygroundEntityPanel {...props} />);

    const btn = screen.getByTestId('playground-redact-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(false);
    expect(btn.textContent).toContain('先匿名化已处理的 54/86 页');
    expect(btn.getAttribute('data-subset')).toBe('true');

    fireEvent.click(btn);
    expect(props.onRedact).toHaveBeenCalledTimes(1);
  });

  it('非子集口径：行为逐字不变（识别中 0 勾选 ⇒ disabled + 「正在处理」文案）', () => {
    const props = { ...baseProps };
    render(<PlaygroundEntityPanel {...props} />);

    const btn = screen.getByTestId('playground-redact-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).toContain('处理中');
    expect(btn.getAttribute('data-subset')).toBeNull();

    fireEvent.click(btn);
    expect(props.onRedact).not.toHaveBeenCalled();
  });

  it('子集页=总页数（识别即将收尾）不启用子集口径，回落普通入口', () => {
    const props = { ...baseProps, subsetReadyCount: 86, subsetTotalPages: 86 };
    render(<PlaygroundEntityPanel {...props} />);

    const btn = screen.getByTestId('playground-redact-btn') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(btn.textContent).not.toContain('先匿名化');
  });

  it('子集入口下不再渲染「无法执行」的红字提示（它按普通口径判定会误报）', () => {
    const props = {
      ...baseProps,
      recognitionIssue: null,
      subsetReadyCount: 54,
      subsetTotalPages: 86,
    };
    render(<PlaygroundEntityPanel {...props} />);

    expect(screen.queryByTestId('playground-redact-disabled-reason')).toBeNull();
  });
});
describe('2026-11-16：主按钮文案与状态对齐 + 子集选页入口', () => {
  it('识别在飞且拿到任务进度 ⇒ 文案是「识别中 12/86」而不是笼统的「处理中...」', () => {
    const props = {
      ...baseProps,
      recognitionProgress: { current: 12, total: 86 },
    };
    render(<PlaygroundEntityPanel {...props} />);

    const btn = screen.getByTestId('playground-redact-btn');
    expect(btn.textContent).toContain('识别中 12/86');
    expect(btn.textContent).not.toContain('处理中');
  });

  it('子集口径点击改走 onSubsetAction（宿主弹选页），onRedact 不被直接调用', () => {
    const props = {
      ...baseProps,
      subsetReadyCount: 54,
      subsetTotalPages: 86,
      onSubsetAction: vi.fn(),
    };
    render(<PlaygroundEntityPanel {...props} />);

    fireEvent.click(screen.getByTestId('playground-redact-btn'));
    expect(props.onSubsetAction).toHaveBeenCalledTimes(1);
    expect(props.onRedact).not.toHaveBeenCalled();
  });

  it('未提供 onSubsetAction 时保持旧行为（直接 onRedact）', () => {
    const props = { ...baseProps, subsetReadyCount: 54, subsetTotalPages: 86 };
    render(<PlaygroundEntityPanel {...props} />);

    fireEvent.click(screen.getByTestId('playground-redact-btn'));
    expect(props.onRedact).toHaveBeenCalledTimes(1);
  });

  it('无任务进度时回落「处理中...」（解析阶段）——不伪造进度数字', () => {
    const props = { ...baseProps, recognitionProgress: null };
    render(<PlaygroundEntityPanel {...props} />);

    expect(screen.getByTestId('playground-redact-btn').textContent).toContain('处理中');
  });
});

describe('2026-11-16：识别在飞但无页级进度（NER/解析段）', () => {
  it('有识别任务 ⇒ 「正在识别...」，不显示笼统的「处理中...」', () => {
    const props = { ...baseProps, recognitionActive: true };
    render(<PlaygroundEntityPanel {...props} />);

    const label = screen.getByTestId('playground-redact-btn').textContent ?? '';
    expect(label).toContain('正在识别');
    expect(label).not.toContain('处理中');
  });

  it('无识别任务（解析阶段）⇒ 回落「处理中...」', () => {
    const props = { ...baseProps, recognitionActive: false };
    render(<PlaygroundEntityPanel {...props} />);

    expect(screen.getByTestId('playground-redact-btn').textContent).toContain('处理中');
  });
});
