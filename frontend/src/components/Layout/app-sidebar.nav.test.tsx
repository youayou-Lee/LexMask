// Copyright 2026 LexMask Contributors

// Issue#262 Task 3：侧边栏法律默认档收敛——渲染级钉。
//
// 钉住的不变量：
// 1) 处理流程组固定 7 项顺序：单次 → 批量 → 供料 → 还原 → 水印 → 任务中心 → 文档中心
//    （Issue#348 追加末位「文档中心」）；
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
      'nav-docs',
      'nav-settings',
    ]);
  });

  // Issue#348：文档中心入口——href 真实存在（不是 filterHiddenNavItems 漏掉的幻影），
  // 且渲染位置在「工作流」组末位（配置组之前）。
  it('侧边栏含文档中心入口（Issue#348）', () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    renderSidebar();
    const link = document.querySelector<HTMLAnchorElement>('a[href="/docs"]');
    expect(link).not.toBeNull();
    expect(link?.getAttribute('data-testid')).toBe('nav-docs');
    const navTestIds = Array.from(document.querySelectorAll('nav [data-testid]')).map((el) =>
      el.getAttribute('data-testid'),
    );
    expect(navTestIds.indexOf('nav-docs')).toBe(navTestIds.indexOf('nav-jobs') + 1);
    expect(navTestIds.indexOf('nav-docs')).toBeLessThan(navTestIds.indexOf('nav-settings'));
  });

  // Issue#348（fix r1）：/docs 是「重定向入口 + /docs/:slug 子路由」——
  // 入口不能带 end，否则 pathname=/docs/test-doc 时 isNavActive 的等值判断失败，
  // 用户进了文档中心菜单项反而永不高亮。高亮断言双轨：aria-current（语义）
  // + data-active（sidebar 的 isActive 入参，真正驱动高亮 class 的那个属性）。
  it('文档中心入口在 /docs 与 /docs/:slug 下均高亮（Issue#348）', () => {
    vi.mocked(listActiveSingleJobs).mockResolvedValue([]);
    for (const path of ['/docs', '/docs/test-doc']) {
      const { unmount } = renderSidebar(path);
      const link = document.querySelector<HTMLAnchorElement>('a[href="/docs"]');
      expect(link, `pathname=${path}：应渲染 /docs 入口`).not.toBeNull();
      expect(link?.getAttribute('aria-current'), `pathname=${path}：aria-current`).toBe('page');
      expect(link?.getAttribute('data-active'), `pathname=${path}：data-active`).toBe('true');
      // 对照组：/docs 前缀匹配不得外溢到同组其它入口（任务中心保持非高亮）。
      const jobsLink = document.querySelector<HTMLAnchorElement>('a[href="/jobs"]');
      expect(jobsLink?.getAttribute('aria-current'), `pathname=${path}：/jobs 不应高亮`).toBeNull();
      expect(jobsLink?.getAttribute('data-active'), `pathname=${path}：/jobs data-active`).toBe(
        'false',
      );
      unmount();
    }
  });
});
