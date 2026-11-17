// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#351 P2（v2 方案，冷审修订后）：侧边栏服务面板降级 + 供料副标题缩短。
//
// D1：SidebarFooter 只留「本地服务」标题+刷新按钮（与 AppHeader 重复的
//     总体状态点删除——header 已有 health-indicator；明细/GPU 显存/版本行
//     移出侧边栏，明细归宿=系统设置→服务监控）。
// D2：nav.agentMd.sub 缩短（22 字在 240px 栏宽必截断 → 短语）。
//
// 钉法（冷审）：完整 health 夹具（三服务 online+2 卡 GPU+runtime_mode）——
// health:null 下负向断言恒真；明细行负向配「刷新按钮正向」；状态行断言
// 无链接（防误加指向不存在目的地的 href）。

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { AppSidebar } from './app-sidebar';
import { useI18n } from '@/i18n';
import { AdminMonitoringPanel } from '@/features/settings/panels/monitoring-panel';
import { SidebarProvider } from '@/components/ui/sidebar';
import { SingleTaskProvider } from '@/features/single-task/single-task-context';

const healthState = vi.hoisted(() => ({
  health: null as Record<string, unknown> | null,
  checking: false,
  roundTripMs: null as number | null,
  refresh: vi.fn(),
}));

vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => false }));
vi.mock('@/features/auth/auth-context', () => ({ useAuth: () => ({ status: null }) }));
vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => healthState,
}));
vi.mock('@/services/singleTaskApi', () => ({ listActiveSingleJobs: vi.fn().mockResolvedValue([]) }));
vi.mock('@/services/jobsApi', () => ({ getJob: vi.fn() }));
vi.mock('@/lib/notifications', () => ({ notifyDone: vi.fn() }));
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));

// 完整 health 夹具（冷审 I3）：三服务 online + runtime_mode=gpu + 2 卡显存——
// 缺了这些，明细行/显存行根本不渲染，负向断言会假绿。
const fullHealth = {
  all_online: true,
  accelerator: 'gpu',
  gpu_memory_all: [
    { index: 0, used_mb: 2048, total_mb: 8192 },
    { index: 1, used_mb: 1024, total_mb: 8192 },
  ],
  services: {
    paddle_ocr: { name: 'ocr', status: 'online', detail: { runtime_mode: 'gpu' } },
    has_ner: { name: 'ner', status: 'online', detail: { runtime_mode: 'gpu' } },
    visual_features: { name: 'vl', status: 'online', detail: { runtime_mode: 'gpu' } },
  },
};

function renderSidebar(initialPath = '/') {
  return render(
    <MemoryRouter initialEntries={[initialPath]}>
      <SidebarProvider>
        <SingleTaskProvider>
          <AppSidebar />
        </SingleTaskProvider>
      </SidebarProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // locale 锁 zh（冷审 I6：测试环境默认 locale=en，副标题断言会打错半）
  useI18n.setState({ locale: 'zh' });
});

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  healthState.health = null;
  healthState.checking = false;
  healthState.roundTripMs = null;
});

describe('Issue#351 P2-D1：侧边栏服务面板降级', () => {
  it('明细行/GPU 显存行/版本行清零（负向，完整夹具）；标题+刷新按钮在场（正向）', () => {
    healthState.health = fullHealth;
    const view = renderSidebar();
    const footer = screen.getByTestId('sidebar-service-status');
    const txt = footer.textContent ?? '';
    // 负向：明细/显存/版本行清零（完整夹具下这些行渲染过 ⇒ 基线必红）
    expect(txt).not.toContain('文字识别服务');
    expect(txt).not.toContain('文本语义服务');
    expect(txt).not.toContain('视觉特征服务');
    expect(txt).not.toContain('GB');
    // testid 配对（门⑥ I-1）：版本字面量在 CI `npm test` 下=1.0.0，v0.0.0 字面量恒绿。
    expect(footer.querySelector('[data-testid="app-version"]')).toBeNull();
    // 正向：标题与刷新按钮在场可点
    expect(txt).toContain('本地服务');
    const refresh = screen.getByTestId('health-refresh');
    expect(refresh).toBeTruthy();
    refresh.click();
    expect(healthState.refresh).toHaveBeenCalledTimes(1);
    // 状态行无链接（冷审 C2：目的地不存在，防误加 href）
    const section = footer.closest('section');
    expect(section?.querySelector('a')).toBeNull();
    view.unmount();
  });

  it('供料模式副标题为短句（D2；zh 默认 locale 由测试环境锁定见下）', () => {
    healthState.health = null;
    const view = renderSidebar();
    // 副标题短句在场（旧 22 字长句清零）
    expect(screen.queryByText(/案卷转脱敏 Markdown，安全供给云端 AI/)).toBeNull();
    // 正向：短版在场（截断根治=短语）
    expect(screen.getAllByText(/案卷转脱敏 Markdown/).length).toBeGreaterThan(0);
    view.unmount();
  });
});
describe('Issue#351 P2-D1 续：版本行正向落位（门⑥ I-2）', () => {
  it('monitoring-panel 渲染版本行（monitoring-build-info 在场）', () => {
    healthState.health = fullHealth;
    healthState.roundTripMs = 12;
    render(<AdminMonitoringPanel />);
    expect(screen.getByTestId('monitoring-build-info')).not.toBeNull();
  });
});
