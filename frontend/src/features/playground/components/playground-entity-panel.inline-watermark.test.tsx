// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render, screen } from '@testing-library/react';
import {
  PlaygroundEntityPanel,
  type PlaygroundEntityPanelProps,
} from './playground-entity-panel';
import type { Entity } from '../types';

/**
 * Issue#338 T11/T12：docline 内嵌水印过滤的**前端可见面**契约。
 *
 * 为什么要有这个提示：docline 曾静默误删正文（真卷 149 页 40.6%），
 * 且 Issue#250 的验收门②（页格标注水印来源）从未交付——用户/律师看不到
 * 「系统动了文档结构」。判据方向（按方案 §3.4）：
 * - `watermark_inline_filtered`（单位=**行**）为正 ⇒ 必须出现 `watermark-inline-notice`；
 * - 缺失 / `null` / `0` / 负数 / NaN ⇒ 一律不出现（「没测到」不得伪装成「有水印」）；
 * - 同时挂视觉段提示时两条**并列**展示（不能互相顶掉）。
 */

const entity: Entity = {
  id: 'e1', text: '张三', type: 'PERSON', start: 0, end: 2, selected: true, source: 'has',
};

const makeProps = (
  overrides: Partial<PlaygroundEntityPanelProps> = {},
): PlaygroundEntityPanelProps => ({
  isImageMode: false,
  isLoading: false,
  recognitionIssue: null,
  visualSkippedNotice: null,
  inlineWatermarkNotice: null,
  entities: [entity],
  mappingEntities: [entity],
  entityTypes: [],
  visionTypes: [],
  visibleBoxes: [],
  selectedCount: 1,
  displaySelectedCount: 1,
  displayTotalCount: 1,
  displayStats: { PERSON: { total: 1, selected: 1 } },
  processingMode: 'mask',
  setProcessingMode: () => {},
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

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#338 T11/T12：内嵌水印行数提示', () => {
  it('T11-1：给了行数 ⇒ 出现提示且文案带行数', () => {
    render(<PlaygroundEntityPanel {...makeProps({ inlineWatermarkNotice: '已从文档结构层剔除 150 行平铺水印文本（正文内容不受影响）' })} />);
    const notice = screen.getByTestId('watermark-inline-notice');
    expect(notice).toBeTruthy();
    expect(notice.textContent).toContain('150');
  });

  it('T11-2：null ⇒ 不出现（「没测到」不得伪装成 0 行或有水印）', () => {
    render(<PlaygroundEntityPanel {...makeProps({ inlineWatermarkNotice: null })} />);
    expect(screen.queryByTestId('watermark-inline-notice')).toBeNull();
  });

  it('T11-3：undefined ⇒ 不出现（后端字段缺失的向后兼容）', () => {
    render(<PlaygroundEntityPanel {...makeProps()} />);
    expect(screen.queryByTestId('watermark-inline-notice')).toBeNull();
  });

  it('T11-4：空字符串 ⇒ 不出现（不得渲染空提示条）', () => {
    render(<PlaygroundEntityPanel {...makeProps({ inlineWatermarkNotice: '' })} />);
    expect(screen.queryByTestId('watermark-inline-notice')).toBeNull();
  });

  it('T12-1：与视觉段提示并列展示（两条互不顶掉）', () => {
    render(
      <PlaygroundEntityPanel
        {...makeProps({
          visualSkippedNotice: '视觉服务未启动，本次已跳过印章/签名检测。',
          inlineWatermarkNotice: '已从文档结构层剔除 13 行平铺水印文本。',
        })}
      />,
    );
    expect(screen.getByTestId('visual-skipped-notice').textContent).toContain('印章');
    expect(screen.getByTestId('watermark-inline-notice').textContent).toContain('13');
  });

  it('T12-2：只给视觉提示时，水印提示位不占位', () => {
    render(
      <PlaygroundEntityPanel
        {...makeProps({ visualSkippedNotice: '视觉服务未启动。', inlineWatermarkNotice: null })}
      />,
    );
    expect(screen.getByTestId('visual-skipped-notice')).toBeTruthy();
    expect(screen.queryByTestId('watermark-inline-notice')).toBeNull();
  });

  it('T12-3：只有水印提示时，视觉提示位不占位', () => {
    render(
      <PlaygroundEntityPanel
        {...makeProps({
          visualSkippedNotice: null,
          inlineWatermarkNotice: '已从文档结构层剔除 2 行平铺水印文本。',
        })}
      />,
    );
    expect(screen.queryByTestId('visual-skipped-notice')).toBeNull();
    expect(screen.getByTestId('watermark-inline-notice')).toBeTruthy();
  });
});