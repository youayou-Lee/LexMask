// Copyright 2026 LexMask Contributors

import { Outlet, useLocation } from 'react-router-dom';
import type { CSSProperties } from 'react';

import { ToastContainer } from '@/components/Toast';
import { OfflineBanner } from '@/components/OfflineBanner';
import { OnboardingGuide } from '@/components/OnboardingGuide';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { PlaygroundProvider } from '@/features/playground/playground-context';
import { SingleTaskProvider } from '@/features/single-task/single-task-context';
import { useUiTheme } from '@/theme/ui-theme';

import { AppSidebar } from './app-sidebar';
import { AppHeader } from './app-header';

export function Layout() {
  const location = useLocation();
  // Issue#355：ds 基线侧边栏 246px（design-proto 定稿）；classic 维持存量 17.5rem。
  // 走 JS 而非 CSS 覆盖：SidebarProvider 侧是 inline style，CSS 无法干净压过。
  const theme = useUiTheme((s) => s.theme);
  const sidebarWidth = theme === 'ds' ? '15.375rem' : '17.5rem';

  return (
    <SidebarProvider
      className="h-dvh min-h-0 overflow-hidden"
      style={{ '--sidebar-width': sidebarWidth } as CSSProperties}
    >
      {/* Issue#194 Bug#1：SingleTaskProvider 必须在 PlaygroundProvider 外侧——
          PlaygroundProvider 自身渲染即调用 usePlayground()/usePlaygroundFile()，
          二者消费 useSingleTask()，Context 只向下游可见；反序会全路由白屏。 */}
      <SingleTaskProvider>
        {/* PlaygroundProvider 必须在 <main key> 之外，路由切换时才不会重建 Provider/会话状态。 */}
        <PlaygroundProvider>
          <OfflineBanner />
          <AppSidebar />
          <SidebarInset className="h-dvh overflow-hidden lg:h-[calc(100dvh-1rem)]">
            <AppHeader />
            <main
              key={location.pathname}
              className="flex min-h-0 flex-1 flex-col overflow-hidden animate-fade-in"
            >
              <Outlet />
            </main>
          </SidebarInset>

          <ToastContainer />
          <OnboardingGuide />
        </PlaygroundProvider>
      </SingleTaskProvider>
    </SidebarProvider>
  );
}

export default Layout;
