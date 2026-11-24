// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#355 登录页双轨钉：
// - ds：三栏海报构图（品牌主张 / 视口居中登录卡 / 脱敏文书叠卡 + 顶栏页脚），切换键在场；
// - classic：原绿渐变白卡登录页（form + max-w-md 卡），无 ds 骨架；
// - 双轨表单主体共用 renderCardBody（防两份 JSX 漂移）。

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AuthPage } from './auth-page';
import { useI18n } from '@/i18n';
import { useUiTheme } from '@/theme/ui-theme';

vi.mock('./auth-context', () => ({
  useAuth: () => ({
    status: { auth_enabled: true, authenticated: false, password_set: true, username: null },
    login: vi.fn(),
    setup: vi.fn(),
    register: vi.fn(),
    logout: vi.fn(),
  }),
}));

// license/status 公开端点：测试里直接 404，不显示横幅
vi.stubGlobal(
  'fetch',
  vi.fn(() => Promise.resolve({ ok: false, json: () => Promise.resolve({}) })),
);

function renderAuth() {
  return render(
    <MemoryRouter initialEntries={['/auth']}>
      <AuthPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
  useUiTheme.setState({ theme: 'ds' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('Issue#355 登录页双轨', () => {
  it('ds：三栏构图在场，登录卡严格居中列，表单在玻璃卡内可提交', () => {
    const { container } = renderAuth();
    // 三栏舞台：左主张 / 中卡 / 右文书叠卡
    expect(container.querySelector('.ds-auth-stage')).toBeTruthy();
    expect(container.querySelector('.ds-auth-claim')).toBeTruthy();
    expect(container.querySelector('.ds-auth-claim-title')?.textContent).toContain('只隐其名');
    expect(container.querySelector('.ds-auth-claim-title')?.textContent).toContain('不失其事');
    expect(container.querySelector('.ds-auth-card-wrap')).toBeTruthy();
    expect(container.querySelector('.ds-auth-docs')).toBeTruthy();
    expect(container.querySelector('.ds-auth-doc-front')).toBeTruthy();
    expect(container.querySelector('.ds-auth-doc-back')).toBeTruthy();
    // 顶栏切换键（登录前也可切）
    expect(container.querySelector('.ds-auth-topbar')).toBeTruthy();
    expect(screen.getByRole('button', { name: /切换经典风格/ })).toBeTruthy();
    // 表单主体（双轨共用）在卡内
    expect(screen.getByLabelText('用户名')).toBeTruthy();
    expect(screen.getByLabelText('密码')).toBeTruthy();
    expect(screen.getByRole('button', { name: '进入系统' })).toBeTruthy();
  });

  it('classic：原绿渐变白卡登录页，无 ds 骨架（零新 DOM）', () => {
    useUiTheme.setState({ theme: 'classic' });
    const { container } = renderAuth();
    expect(container.querySelector('.ds-auth-stage')).toBeNull();
    expect(container.querySelector('.ds-auth-claim')).toBeNull();
    expect(container.querySelector('.ds-auth-docs')).toBeNull();
    // classic 表单在原白卡里
    expect(screen.getByLabelText('用户名')).toBeTruthy();
    expect(screen.getByLabelText('密码')).toBeTruthy();
    expect(container.querySelector('.max-w-md')).toBeTruthy();
  });
});