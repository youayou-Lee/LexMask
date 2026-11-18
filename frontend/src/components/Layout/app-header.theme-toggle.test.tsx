// Copyright 2026 LexMask Contributors

// Issue#355 验收方案 v2 ①3：顶栏风格切换按钮。
// 钉住的不变量：
// 1) 顶栏 pill nav 内存在 theme-toggle 按钮（语言切换旁）；
// 2) 点击 → 根节点 data-theme 翻转 + localStorage 持久化（机制面，探针⑤的 jsdom 面）；
// 3) aria-pressed 表达 ds 选中态、aria-label 随目标主题切换（无障碍名随状态）。

// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppHeader } from './app-header';
import { SidebarProvider } from '@/components/ui/sidebar';
import { STORAGE_KEYS } from '@/constants/storage-keys';
import { useUiTheme } from '@/theme/ui-theme';

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, checking: false, roundTripMs: null, refresh: vi.fn() }),
}));
vi.mock('@/features/auth/auth-context', () => ({ useAuth: () => ({ status: null, logout: vi.fn() }) }));
vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => false }));

function renderHeader() {
  return render(
    <SidebarProvider>
      <MemoryRouter>
        <AppHeader />
      </MemoryRouter>
    </SidebarProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
  useUiTheme.setState({ theme: 'ds' });
});

afterEach(() => {
  cleanup();
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

describe('Issue#355 顶栏风格切换按钮', () => {
  it('存在 theme-toggle 且 ds 态 aria-pressed=true、目标文案=经典风格', () => {
    renderHeader();
    const btn = screen.getByTestId('theme-toggle');
    expect(btn).toBeTruthy();
    expect(btn.getAttribute('aria-pressed')).toBe('true');
    expect(btn.getAttribute('aria-label')).toBe('切换经典风格');
  });

  it('点击 → 根节点翻 classic + 持久化 + aria 状态反转；再点回 ds', () => {
    renderHeader();
    const btn = screen.getByTestId('theme-toggle');

    fireEvent.click(btn);
    expect(document.documentElement.dataset.theme).toBe('classic');
    expect(localStorage.getItem(STORAGE_KEYS.UI_THEME)).toBe('classic');
    expect(useUiTheme.getState().theme).toBe('classic');
    expect(btn.getAttribute('aria-pressed')).toBe('false');
    expect(btn.getAttribute('aria-label')).toBe('切换新版风格');

    fireEvent.click(btn);
    expect(document.documentElement.dataset.theme).toBe('ds');
    expect(localStorage.getItem(STORAGE_KEYS.UI_THEME)).toBe('ds');
    expect(btn.getAttribute('aria-pressed')).toBe('true');
  });
});
