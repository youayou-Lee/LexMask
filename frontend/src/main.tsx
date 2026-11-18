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
