// Copyright 2026 LexMask Contributors

// Issue#355 主题双轨状态机（验收方案 v2 ①2）：
// - 无存储值默认 ds（新风格默认进入）；
// - classic 持久化往返（刷新后保持）；
// - 非法存储值回落 ds（fail-closed 到新基线）；
// - initUiTheme 于渲染前把主题刷到 <html data-theme>（FOUC 防护）；
// - setTheme 同步根节点属性 + localStorage（切换即时生效探针的机制面）。

// @vitest-environment jsdom

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { STORAGE_KEYS } from '@/constants/storage-keys';

type ThemeModule = typeof import('./ui-theme');

/** store 是模块级单例：每例 resetModules + 动态 import 取干净状态。 */
async function fresh(): Promise<ThemeModule> {
  vi.resetModules();
  return import('./ui-theme');
}

beforeEach(() => {
  localStorage.clear();
  delete document.documentElement.dataset.theme;
});

afterEach(() => {
  delete document.documentElement.dataset.theme;
});

describe('Issue#355 ui-theme 双轨主题状态机', () => {
  it('无存储值默认 ds（新风格默认进入）', async () => {
    const m = await fresh();
    expect(m.useUiTheme.getState().theme).toBe('ds');
  });

  it('持久化 classic → 新实例读回 classic；非法值回落 ds', async () => {
    localStorage.setItem(STORAGE_KEYS.UI_THEME, 'classic');
    let m = await fresh();
    expect(m.useUiTheme.getState().theme).toBe('classic');

    localStorage.setItem(STORAGE_KEYS.UI_THEME, 'neon');
    m = await fresh();
    expect(m.useUiTheme.getState().theme).toBe('ds');
  });

  it('toggleTheme 翻转并持久化（ds→classic→ds 往返）', async () => {
    const m = await fresh();
    m.useUiTheme.getState().toggleTheme();
    expect(m.useUiTheme.getState().theme).toBe('classic');
    expect(localStorage.getItem(STORAGE_KEYS.UI_THEME)).toBe('classic');
    m.useUiTheme.getState().toggleTheme();
    expect(m.useUiTheme.getState().theme).toBe('ds');
    expect(localStorage.getItem(STORAGE_KEYS.UI_THEME)).toBe('ds');
  });

  it('initUiTheme 渲染前刷根节点：无值→ds、classic→classic、非法→ds（FOUC 防护）', async () => {
    let m = await fresh();
    m.initUiTheme();
    expect(document.documentElement.dataset.theme).toBe('ds');

    localStorage.setItem(STORAGE_KEYS.UI_THEME, 'classic');
    m = await fresh();
    m.initUiTheme();
    expect(document.documentElement.dataset.theme).toBe('classic');

    localStorage.setItem(STORAGE_KEYS.UI_THEME, 'garbage');
    m = await fresh();
    m.initUiTheme();
    expect(document.documentElement.dataset.theme).toBe('ds');
  });

  it('setTheme 同步写根节点 data-theme 属性（classic 显式落属性，可被 inspect）', async () => {
    const m = await fresh();
    m.useUiTheme.getState().setTheme('classic');
    expect(document.documentElement.dataset.theme).toBe('classic');
    m.useUiTheme.getState().setTheme('ds');
    expect(document.documentElement.dataset.theme).toBe('ds');
  });
});
