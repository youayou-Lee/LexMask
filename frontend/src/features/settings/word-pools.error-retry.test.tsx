// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#267 A5：words 页签失败态 Alert 带重试按钮——word-pools 自带本地 load callback，
// 服务层 fetchWordPools mock 为先拒绝后成功，钉「失败 Alert + 重试重发」。M-族变异删靶 =
// 删该重试按钮 → 本用例红。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useI18n } from '@/i18n';
import { WordPoolsSettings } from './word-pools';

const fetchWordPoolsMock = vi.hoisted(() => vi.fn());

vi.mock('@/services/wordPoolsApi', () => ({
  fetchWordPools: fetchWordPoolsMock,
}));

afterEach(() => {
  cleanup();
  fetchWordPoolsMock.mockReset();
});

describe('Issue#267：words 页签失败态重试', () => {
  it('A5：首载失败 → loadError Alert 可见；点重试 → fetchWordPools 重发且成功后 Alert 退场', async () => {
    useI18n.setState({ locale: 'zh' });
    fetchWordPoolsMock.mockRejectedValueOnce(new Error('boom'));
    fetchWordPoolsMock.mockResolvedValueOnce({ merged: {}, overrides: {} });
    render(<WordPoolsSettings />);

    await waitFor(() => {
      expect(fetchWordPoolsMock).toHaveBeenCalledTimes(1);
    });
    expect(screen.getByTestId('word-pools-load-error-retry')).toBeTruthy();

    fireEvent.click(screen.getByTestId('word-pools-load-error-retry'));
    await waitFor(() => {
      expect(fetchWordPoolsMock).toHaveBeenCalledTimes(2);
    });
    await waitFor(() => {
      expect(screen.queryByTestId('word-pools-load-error-retry')).toBeNull();
    });
  });
});
