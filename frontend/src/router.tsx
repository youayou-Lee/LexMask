// Copyright 2026 LexMask Contributors

import React from 'react';
import { createBrowserRouter, Navigate, useLocation, useParams } from 'react-router-dom';
import { Button } from './components/ui/button';
import { Layout } from './components/Layout';
import { ErrorBoundary } from './components/ErrorBoundary';
import { useT } from './i18n';
import { SUSPENSE_SPINNER_DELAY_MS, ROUTE_PREFETCH_DELAY_MS } from './constants/timing';
import { isBatchWizardMode } from './constants/batch-wizard-modes';
import { isRegisterAuthSearch, resolveAuthNext } from './features/auth/auth-routing';
import { useAuth } from './features/auth/auth-context';

const AuthPage = React.lazy(() =>
  import('./features/auth/auth-page').then((m) => ({ default: m.AuthPage })),
);
const Playground = React.lazy(() =>
  import('./features/playground').then((m) => ({ default: m.Playground })),
);
const Batch = React.lazy(() => import('./features/batch').then((m) => ({ default: m.Batch })));
const BatchHub = React.lazy(() =>
  import('./features/batch').then((m) => ({ default: m.BatchHub })),
);
const TasksPage = React.lazy(() =>
  import('./features/jobs').then((m) => ({ default: m.TasksPage })),
);
const JobDetailPage = React.lazy(() =>
  import('./features/jobs').then((m) => ({ default: m.JobDetailPage })),
);
const Structured = React.lazy(() =>
  import('./features/structured/pages/overview').then((m) => ({ default: m.Structured })),
);
const StructuredFiles = React.lazy(() =>
  import('./features/structured/pages/files').then((m) => ({ default: m.StructuredFiles })),
);
const StructuredDatabase = React.lazy(() =>
  import('./features/structured/pages/database').then((m) => ({ default: m.StructuredDatabase })),
);
const StructuredDatasets = React.lazy(() =>
  import('./features/structured/pages/datasets').then((m) => ({ default: m.StructuredDatasets })),
);
const StructuredDelivery = React.lazy(() =>
  import('./features/structured/pages/delivery').then((m) => ({ default: m.StructuredDelivery })),
);
const DicomWorkspace = React.lazy(() =>
  import('./features/dicom').then((m) => ({ default: m.DicomWorkspace })),
);
const Settings = React.lazy(() =>
  import('./features/settings').then((m) => ({ default: m.Settings })),
);
const SystemSettings = React.lazy(() =>
  import('./features/settings').then((m) => ({ default: m.SystemSettings })),
);
const TextModelSettings = React.lazy(() =>
  import('./features/settings').then((m) => ({ default: m.TextModelSettings })),
);
const VisionModelSettings = React.lazy(() =>
  import('./features/settings').then((m) => ({ default: m.VisionModelSettings })),
);
const PlaygroundImagePopout = React.lazy(() =>
  import('./features/playground/components/playground-image-popout').then((m) => ({
    default: m.PlaygroundImagePopout,
  })),
);
const ConsolePage = React.lazy(() =>
  import('./features/console/console-page').then((m) => ({ default: m.ConsolePage })),
);
const AgentMd = React.lazy(() =>
  import('./features/agent-md').then((m) => ({ default: m.AgentMd })),
);
const Restore = React.lazy(() =>
  import('./features/restore').then((m) => ({ default: m.RestorePage })),
);
const WatermarkTool = React.lazy(() =>
  import('./features/watermark-tool').then((m) => ({ default: m.WatermarkToolPage })),
);

function DelayedSpinner() {
  const [show, setShow] = React.useState(false);

  React.useEffect(() => {
    const timer = setTimeout(() => setShow(true), SUSPENSE_SPINNER_DELAY_MS);
    return () => clearTimeout(timer);
  }, []);

  if (!show) return null;

  return (
    <div className="flex h-full items-center justify-center animate-fade-in">
      <div className="h-7 w-7 animate-spin rounded-full border-2 border-border border-t-foreground" />
    </div>
  );
}

const SuspenseFallback = <DelayedSpinner />;

function FullPageSpinner() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-background">
      <DelayedSpinner />
    </div>
  );
}

function AuthStatusErrorPage() {
  const t = useT();
  const { error, refresh } = useAuth();

  return (
    <div className="flex min-h-dvh items-center justify-center bg-background px-6">
      <div className="w-full max-w-md rounded-3xl border border-border/70 bg-card p-6 shadow-[var(--shadow-md)]">
        <h1 className="text-lg font-semibold tracking-tight text-foreground">
          {t('networkFallback.networkTitle')}
        </h1>
        <p className="mt-2 text-sm leading-6 text-muted-foreground">
          {error || t('networkFallback.networkDesc')}
        </p>
        <Button className="mt-5" onClick={() => void refresh()}>
          {t('common.retry')}
        </Button>
      </div>
    </div>
  );
}

const prefetchRoutes = () => {
  const routes = [
    () => import('./features/playground'),
    () => import('./features/batch'),
    () => import('./features/structured'),
    () => import('./features/dicom'),
    // features/history 不单列：tasks-page（features/jobs）已静态 import HistoryView，
    // 预取 jobs 时随 chunk 一并拉下（Issue#262 A1 合并页后冗余）。
    () => import('./features/jobs'),
  ];

  void Promise.allSettled(routes.map((route) => route()));
};

if (typeof window !== 'undefined') {
  if ('requestIdleCallback' in window) {
    (
      window as Window & {
        requestIdleCallback: (callback: () => void, options?: { timeout?: number }) => number;
      }
    ).requestIdleCallback(prefetchRoutes, { timeout: 1200 });
  } else {
    setTimeout(prefetchRoutes, Math.min(ROUTE_PREFETCH_DELAY_MS, 400));
  }
}

function LazyPage({ children }: { children: React.ReactNode }) {
  return (
    <ErrorBoundary>
      <React.Suspense fallback={SuspenseFallback}>{children}</React.Suspense>
    </ErrorBoundary>
  );
}

function getNextLocation(pathname: string, search: string, hash: string): string {
  return `${pathname}${search}${hash}`;
}

function RequireAuth({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const { loading, status } = useAuth();

  if (loading) return <FullPageSpinner />;
  if (!status) return <AuthStatusErrorPage />;

  if (status.auth_enabled && !status.authenticated) {
    const next = encodeURIComponent(
      getNextLocation(location.pathname, location.search, location.hash),
    );
    return <Navigate to={`/auth?next=${next}`} replace />;
  }

  return <>{children}</>;
}

function AuthRoute() {
  const location = useLocation();
  const { loading, status } = useAuth();
  const next = resolveAuthNext(location.search);
  const isRegisterRoute = isRegisterAuthSearch(location.search);

  if (loading) return <FullPageSpinner />;
  if (!status) return <AuthStatusErrorPage />;
  if (!status.auth_enabled || status.authenticated) {
    // Issue#262 D6：注册深链登录后落单次处理（开始页已删，「home」语义过时，
    // 改名 isRegisterLanding——行为等价：仅当已登录且来自注册链接时落 /single）。
    const isRegisterLanding = status.authenticated && isRegisterRoute;
    return <Navigate to={isRegisterLanding ? '/single' : next} replace />;
  }

  return (
    <ErrorBoundary>
      <React.Suspense fallback={<FullPageSpinner />}>
        <AuthPage />
      </React.Suspense>
    </ErrorBoundary>
  );
}

// Issue#264：导出供路由行为契约测试直挂真实组件（纯导出，零行为差异）。
// 放行白名单收敛到单一来源常量（constants/batch-wizard-modes）——与导航侧
// resolveBatchRouteMode 共用同一份，杜绝「导航产物被路由弹回」的口径漂移。
export function BatchRoute() {
  const { batchMode } = useParams();
  if (!isBatchWizardMode(batchMode)) {
    return <Navigate to="/batch" replace />;
  }
  return (
    <LazyPage>
      <Batch key={batchMode} />
    </LazyPage>
  );
}

function StructuredPoliciesCompatRoute() {
  const location = useLocation();
  return <Navigate to={`/structured/datasets${location.search}${location.hash}`} replace />;
}

export const router = createBrowserRouter([
  {
    path: '/setup',
    element: <Navigate to="/auth" replace />,
  },
  {
    path: '/auth',
    element: <AuthRoute />,
  },
  {
    path: '/playground/image-editor',
    element: (
      <RequireAuth>
        <LazyPage>
          <PlaygroundImagePopout />
        </LazyPage>
      </RequireAuth>
    ),
  },
  {
    path: '/',
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      {
        index: true,
        element: <Navigate to="/single" replace />,
      },
      {
        path: 'single',
        element: (
          <LazyPage>
            <Playground />
          </LazyPage>
        ),
      },
      {
        path: 'playground',
        element: (
          <LazyPage>
            <Playground />
          </LazyPage>
        ),
      },
      {
        path: 'batch',
        element: (
          <LazyPage>
            <BatchHub />
          </LazyPage>
        ),
      },
      { path: 'batch/:batchMode', element: <BatchRoute /> },
      {
        path: 'agent-md',
        element: (
          <LazyPage>
            <AgentMd />
          </LazyPage>
        ),
      },
      {
        path: 'structured',
        element: (
          <LazyPage>
            <Structured />
          </LazyPage>
        ),
      },
      {
        path: 'structured/files',
        element: (
          <LazyPage>
            <StructuredFiles />
          </LazyPage>
        ),
      },
      {
        path: 'structured/database',
        element: (
          <LazyPage>
            <StructuredDatabase />
          </LazyPage>
        ),
      },
      {
        path: 'structured/datasets',
        element: (
          <LazyPage>
            <StructuredDatasets />
          </LazyPage>
        ),
      },
      {
        path: 'structured/policies',
        element: <StructuredPoliciesCompatRoute />,
      },
      {
        path: 'structured/delivery',
        element: (
          <LazyPage>
            <StructuredDelivery />
          </LazyPage>
        ),
      },
      {
        path: 'dicom',
        element: (
          <LazyPage>
            <DicomWorkspace />
          </LazyPage>
        ),
      },
      {
        path: 'restore',
        element: (
          <LazyPage>
            <Restore />
          </LazyPage>
        ),
      },
      {
        path: 'watermark-tool',
        element: (
          <LazyPage>
            <WatermarkTool />
          </LazyPage>
        ),
      },
      // Issue#262 A1：/history 并入任务中心合并页——旧深链经重定向透传 source/jobId
      { path: 'history', element: <HistoryRedirect /> },
      {
        path: 'jobs',
        element: (
          <LazyPage>
            <TasksPage />
          </LazyPage>
        ),
      },
      {
        path: 'jobs/:jobId',
        element: (
          <LazyPage>
            <JobDetailPage />
          </LazyPage>
        ),
      },
      {
        path: 'console',
        element: (
          <LazyPage>
            <ConsolePage />
          </LazyPage>
        ),
      },
      {
        path: 'settings/system',
        element: (
          <LazyPage>
            <SystemSettings />
          </LazyPage>
        ),
      },
      // Issue#262 D3：旧设置路由重定向进高级设置单入口对应页签（深链不断；
      // 旧 URL 自身的其他 query 参数允许丢弃，只透传 tab）。
      { path: 'settings/redaction', element: <SettingsTabRedirect tab="redaction" /> },
      { path: 'settings/word-pools', element: <SettingsTabRedirect tab="words" /> },
      {
        path: 'settings',
        element: (
          <LazyPage>
            <Settings />
          </LazyPage>
        ),
      },
      { path: 'model-settings', element: <Navigate to="/model-settings/text" replace /> },
      {
        path: 'model-settings/text',
        element: (
          <LazyPage>
            <TextModelSettings />
          </LazyPage>
        ),
      },
      {
        path: 'model-settings/vision',
        element: (
          <LazyPage>
            <VisionModelSettings />
          </LazyPage>
        ),
      },
      { path: '*', element: <NotFound /> },
    ],
  },
]);

// Issue#262 D3：旧设置路由 → /settings?tab=<tab> 重定向（replace，避免历史栈堆积中间态）
function SettingsTabRedirect({ tab }: { tab: string }) {
  return <Navigate to={`/settings?tab=${tab}`} replace />;
}

// Issue#262 A1：旧 /history 深链 → /jobs?view=done（replace，避免历史栈堆积中间态）；
// source / jobId 等既有 query 原样透传，由 HistoryView 的 use-history-data URL 同步消费。
// 导出仅为渲染测试钉（router.test.tsx）；路由表内使用不受影响。
export function HistoryRedirect() {
  const location = useLocation();
  const params = new URLSearchParams(location.search);
  params.set('view', 'done');
  return <Navigate to={`/jobs?${params.toString()}`} replace />;
}

function NotFound() {
  const t = useT();
  return (
    <div className="flex h-full min-h-0 items-center justify-center px-6">
      <div className="flex min-h-[320px] w-full max-w-xl flex-col items-center justify-center gap-4 rounded-[28px] border border-border/70 bg-card px-8 py-12 text-center shadow-[var(--shadow-floating)]">
        <span className="text-6xl font-semibold tracking-[-0.06em] text-foreground/15">404</span>
        <div className="space-y-2">
          <p className="text-lg font-semibold text-foreground">{t('notFound.title')}</p>
          <p className="text-sm text-muted-foreground">{t('notFound.desc')}</p>
        </div>
        <a
          href="/"
          className="inline-flex h-10 items-center rounded-full border border-border bg-background px-4 text-sm font-medium text-foreground transition-colors hover:bg-accent"
        >
          {t('notFound.backHome')}
        </a>
      </div>
    </div>
  );
}
