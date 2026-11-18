// Copyright 2026 LexMask Contributors

import { Outlet, useLocation } from 'react-router-dom';
import type { CSSProperties } from 'react';

import { ToastContainer } from '@/components/Toast';
import { OfflineBanner } from '@/components/OfflineBanner';
import { OnboardingGuide } from '@/components/OnboardingGuide';
import { SidebarInset, SidebarProvider } from '@/components/ui/sidebar';
import { PlaygroundProvider } from '@/features/playground/playground-context';
import { SingleTaskProvider } from '@/features/single-task/single-task-context';

import { AppSidebar } from './app-sidebar';
import { AppHeader } from './app-header';

export function Layout() {
  const location = useLocation();

  return (
    <SidebarProvider
      className="h-dvh min-h-0 overflow-hidden"
      style={{ '--sidebar-width': '17.5rem' } as CSSProperties}
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
