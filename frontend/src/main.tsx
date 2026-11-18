// Copyright 2026 LexMask Contributors

import React from 'react';
import ReactDOM from 'react-dom/client';
import { QueryClientProvider } from '@tanstack/react-query';
import { RouterProvider } from 'react-router-dom';
import { queryClient } from '@/lib/query-client';
import { AuthProvider } from '@/features/auth/auth-context';
import { router } from './router';
import { ErrorBoundary } from './components/ErrorBoundary';
import { applyBrand } from '@/config/brand';
import { initI18n } from '@/i18n';
import { initUiTheme } from '@/theme/ui-theme';
import './index.css';
// Issue#355：ds 主题（Harvey×Linear 基线）——全部规则自作用域化（[data-theme="ds"]），
// classic（无该属性）零生效；契约测试 src/styles/ds-theme-scope.test.ts 强制。
// 经 main.tsx 引入而非 index.css @import：CSS 规范要求 @import 先于其它规则，
// 这样 index.css 保持零 diff（比「仅 import 行」更强的 classic 零改动保证）。
import './styles/ds-theme.css';
// Issue#355 iOS 重置：B 分区（数据页面）样式——编排者统一接线，顺序在 ds-theme 之后取覆盖
import './styles/ios-pages-b.css';

applyBrand();
// Issue#355：渲染前把持久化主题刷到根节点，首帧即正确风格（防 FOUC）。
initUiTheme();

// Await the initial locale (en is lazy-loaded) so the first paint never flickers.
void initI18n().then(() => {
  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <QueryClientProvider client={queryClient}>
        <AuthProvider>
          <ErrorBoundary>
            <RouterProvider router={router} />
          </ErrorBoundary>
        </AuthProvider>
      </QueryClientProvider>
    </React.StrictMode>,
  );
});
