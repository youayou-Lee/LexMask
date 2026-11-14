// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#267 A4：redaction 页签失败态 Alert 带重试按钮——useRedactionPresets 整体 mock
//（沿 advanced-hub 的 mock 惯例），三个子渲染组件 mock 为占位，只钉「失败 Alert + 重试
// 调 reload」这一条接线。M6 变异删靶 = 删该重试按钮 → 本用例红。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { RedactionList } from './redaction-list';

const reloadMock = vi.hoisted(() => vi.fn(async () => {}));

vi.mock('./hooks/use-redaction-presets', () => ({
  useRedactionPresets: () => ({
    loading: false,
    loadError: '设置加载失败，请检查后端服务或稍后重试。',
    reload: reloadMock,
  }),
}));
vi.mock('./components/preset-column', () => ({
  PresetColumn: () => <div data-testid="mock-preset-column" />,
}));
vi.mock('./redaction-bridge-config', () => ({
  RedactionBridgeConfig: () => <div data-testid="mock-bridge-config" />,
}));
vi.mock('./redaction-preset-dialog', () => ({
  RedactionPresetDialog: () => null,
}));

afterEach(() => {
  cleanup();
  reloadMock.mockClear();
});

describe('Issue#267：redaction 页签失败态重试', () => {
  it('A4：loadError Alert 可见；点重试 → reload 被调恰一次', () => {
    render(<RedactionList />);
    expect(screen.getByText('设置加载失败，请检查后端服务或稍后重试。')).toBeTruthy();
    fireEvent.click(screen.getByTestId('redaction-load-error-retry'));
    expect(reloadMock).toHaveBeenCalledTimes(1);
  });
});
