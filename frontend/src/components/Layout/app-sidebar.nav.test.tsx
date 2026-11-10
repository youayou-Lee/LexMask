// Copyright 2026 LexMask Contributors

// Issue#262 Task 3：侧边栏法律默认档收敛——渲染级钉。
//
// 钉住的不变量：
// 1) 处理流程组固定 6 项顺序：单次 → 批量 → 供料 → 还原 → 水印 → 任务中心；
// 2) 「开始」（/）与「处理结果」（/history）不再出现在侧边栏
//    （/ 已重定向 /single；/history 路由由 Task 5 处理，此处只摘入口）；
// 3) 配置组（非管理员）只剩「高级设置」一项。
//
// filterHiddenNavItems 只认 HIDDEN_NAV_PATHS（/structured、/dicom），滤不掉
// / 与 /history——导航钉只能通过渲染 AppSidebar 从源头断言（mock 骨架同
// sidebar-badge.test.tsx：auth / useServiceHealth / SingleTaskProvider）。

// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { AppSidebar } from './app-sidebar';
import { SidebarProvider } from '@/components/ui/sidebar';
import { SingleTaskProvider } from '@/features/single-task/single-task-context';
import { listActiveSingleJobs } from '@/services/singleTaskApi';

vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => false }));
vi.mock('@/features/auth/auth-context', () => ({ useAuth: () => ({ status: null }) }));
vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({
    health: null,
    checking: false,
    roundTripMs: null,
    refresh: vi.fn(),
  }),
}));
vi.mock('@/services/singleTaskApi', () => ({ listActiveSingleJobs: vi.fn() }));
vi.mock('@/services/jobsApi', () => ({ getJob: vi.fn() }));
vi.mock('@/lib/notifications', () => ({ notifyDone: vi.fn() }));
vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));

function renderSidebar() {
  return render(
    <MemoryRouter>
      <SidebarProvider>
        <SingleTaskProvider>
          <AppSidebar />
        </SingleTaskProvider>
      </SidebarProvider>
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#262：侧边栏法律默认档收敛', () => {
  it('法律默认档：处理流程 6 项固定顺序，开始/处理结果不再出现', () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    renderSidebar();
    expect(screen.queryByTestId('nav-start')).toBeNull();
    expect(screen.queryByTestId('nav-history')).toBeNull();
    expect(screen.getByTestId('nav-jobs')).toBeTruthy();
    // <nav> 内两组按 DOM 顺序拼接：处理流程 6 项 + 配置组（非管理员仅高级设置）。
    const navTestIds = Array.from(document.querySelectorAll('nav [data-testid]')).map((el) =>
      el.getAttribute('data-testid'),
    );
    expect(navTestIds).toEqual([
      'nav-single',
      'nav-batch',
      'nav-agent-md',
      'nav-restore',
      'nav-watermark-tool',
      'nav-jobs',
      'nav-settings',
    ]);
  });
});
