// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// 侧边栏服务面板钉（Issue#355 用户裁定 2026-11-19 翻转 #351 P2-D1）。
//
// 演变史：#351 P2-D1 曾把卡片降级为「标题+刷新」一行；#355 用户验收期裁定
// 「空壳卡无意义」——连接信息（服务明细/显存/版本）回归侧栏常驻。本文件从
// 「降级钉」改写为「恢复钉」，判据方向整体翻转，夹具沿用冷审定稿：
// 完整 health 夹具（三服务 online+2 卡 GPU+runtime_mode）——health:null 下
// 断言恒真；明细行正向配「刷新按钮正向」；状态行断言无链接。
//
// 版本行：侧栏 app-version 与系统设置→服务监控 monitoring-build-info 并存
// （侧栏常驻可见 vs 管理员详情页，两处同源 __APP_VERSION__）。

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

// 完整 health 夹具（沿用冷审 I3）：三服务 online + runtime_mode=gpu + 2 卡显存——
// 缺了这些，明细行/显存行根本不渲染，正向断言会假红。
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

describe('Issue#355：侧边栏服务面板恢复明细（翻转 #351 P2-D1）', () => {
  it('服务明细行/显存行/版本行在场（正向，完整夹具）；标题+刷新按钮可点', () => {
    healthState.health = fullHealth;
    const view = renderSidebar();
    const footer = screen.getByTestId('sidebar-service-status');
    const txt = footer.textContent ?? '';
    // 正向：三服务明细行（恢复的连接信息本体）
    expect(txt).toContain('文字识别服务');
    expect(txt).toContain('文本语义服务');
    expect(txt).toContain('视觉特征服务');
    // 正向：双卡显存（2.0/8.0 与 1.0/8.0 GB）
    expect(txt).toContain('GB');
    // 正向：版本行恢复（testid 配对沿用门⑥ I-1 口径：版本字面量在 CI npm test 下=1.0.0）
    expect(footer.querySelector('[data-testid="app-version"]')).not.toBeNull();
    // 正向：状态行文本（全部服务正常）与刷新按钮在场可点
    expect(txt).toContain('本地服务');
    const refresh = screen.getByTestId('health-refresh');
    expect(refresh).toBeTruthy();
    refresh.click();
    expect(healthState.refresh).toHaveBeenCalledTimes(1);
    // 状态行无链接（冷审 C2 沿用：目的地不存在，防误加 href）
    const section = footer.closest('section');
    expect(section?.querySelector('a')).toBeNull();
    view.unmount();
  });

  it('health=null 且非 checking：降级服务行仍渲染（fallback offline 徽章），不空壳', () => {
    healthState.health = null;
    const view = renderSidebar();
    const footer = screen.getByTestId('sidebar-service-status');
    const txt = footer.textContent ?? '';
    expect(txt).toContain('文字识别服务');
    // health.backendDown 在 zh 存在重复键（后段「不可用」生效，与历史行为一致）
    expect(txt).toContain('不可用');
    view.unmount();
  });

  it('供料模式副标题为短句（#351 P2-D2 维持不变）', () => {
    healthState.health = null;
    const view = renderSidebar();
    expect(screen.queryByText(/案卷转脱敏 Markdown，安全供给云端 AI/)).toBeNull();
    expect(screen.getAllByText(/案卷转脱敏 Markdown/).length).toBeGreaterThan(0);
    view.unmount();
  });
});
describe('版本行正向落位（#351 门⑥ I-2 维持 + #355 侧栏并存）', () => {
  it('monitoring-panel 渲染版本行（monitoring-build-info 在场）', () => {
    healthState.health = fullHealth;
    healthState.roundTripMs = 12;
    render(<AdminMonitoringPanel />);
    expect(screen.getByTestId('monitoring-build-info')).not.toBeNull();
  });
});
