// Copyright 2026 LexMask Contributors

// Issue#355 主题双轨：'ds' = Harvey×Linear 新设计基线（默认），'classic' = 升级前存量视觉。
//
// 机制（设计评论 A1）：根节点 data-theme 属性 + localStorage 持久化。
// 所有新样式集中在 src/styles/ds-theme.css 并以 [data-theme="ds"] 为作用域前缀
// （契约测试 ds-theme-scope.test.ts 强制）——classic = 属性不为 ds ⇒ 存量样式
// 零改动；切换 = 根属性翻转 + 持久化写入，不刷新页面。
//
// 持久化失败（隐私模式等）静默降级：主题仍可在本会话内切换。

import { create } from 'zustand';

import { STORAGE_KEYS } from '@/constants/storage-keys';

export type UiTheme = 'ds' | 'classic';

function isUiTheme(value: unknown): value is UiTheme {
  return value === 'ds' || value === 'classic';
}

function readStoredTheme(): UiTheme {
  try {
    const raw = localStorage.getItem(STORAGE_KEYS.UI_THEME);
    return isUiTheme(raw) ? raw : 'ds';
  } catch {
    return 'ds';
  }
}

function persistTheme(theme: UiTheme): void {
  try {
    localStorage.setItem(STORAGE_KEYS.UI_THEME, theme);
  } catch {
    // 存储不可用时持久化静默失败，会话内切换不受影响。
  }
}

function applyToDocument(theme: UiTheme): void {
  if (typeof document === 'undefined') return;
  document.documentElement.dataset.theme = theme;
}

interface UiThemeStore {
  theme: UiTheme;
  setTheme: (theme: UiTheme) => void;
  toggleTheme: () => void;
}

export const useUiTheme = create<UiThemeStore>((set, get) => ({
  theme: readStoredTheme(),
  setTheme: (theme) => {
    persistTheme(theme);
    applyToDocument(theme);
    set({ theme });
  },
  toggleTheme: () => {
    get().setTheme(get().theme === 'ds' ? 'classic' : 'ds');
  },
}));

/**
 * main.tsx 在首次渲染前调用：把持久化主题刷到根节点，首帧即正确主题（FOUC 防护）。
 */
export function initUiTheme(): void {
  applyToDocument(useUiTheme.getState().theme);
}
