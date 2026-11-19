// Copyright 2026 LexMask Contributors

import { Alert, AlertDescription } from '@/components/ui/alert';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useAuth } from '@/features/auth/auth-context';
import { useUiTheme } from '@/theme/ui-theme';
import { AdminRuntimePanel } from './panels/runtime-panel';
import { AdminAccessPanel } from './panels/access-panel';
import { AdminAuditPanel } from './panels/audit-panel';
import { AdminLicensePanel } from './panels/license-panel';
import { AdminMonitoringPanel } from './panels/monitoring-panel';

export function SystemSettings() {
  const { status } = useAuth();
  // Issue#355 iOS 重置：ds 下标题换 32px 大标题类、页签换 iOS 分段样式、
  // 页签面板走白卡语言（ios-tabpanel，样式仅 ds 生效）；classic 类名原样。
  // 文案维持既有硬编码中文（本页历史口径，不在本轮 i18n 化范围）。
  const dsMode = useUiTheme((s) => s.theme) === 'ds';

  if (!status?.is_super_admin) {
    return (
      <div className="saas-page flex min-h-0 min-w-0 flex-1 flex-col bg-background">
        <div className="page-shell !max-w-[min(100%,1920px)] !px-3 !py-2 sm:!px-4 sm:!py-3">
          <Alert variant="destructive">
            <AlertDescription>需要管理员权限。</AlertDescription>
          </Alert>
        </div>
      </div>
    );
  }

  return (
    <div className="saas-page flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
      <div className="page-shell !max-w-[min(100%,1920px)] !px-3 !py-2 sm:!px-4 sm:!py-3">
        <Tabs defaultValue="runtime" className="page-stack gap-3 overflow-hidden">
          <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
            <div>
              <h1
                className={
                  dsMode
                    ? 'ds-page-title'
                    : 'text-2xl font-semibold tracking-tight text-foreground'
                }
              >
                系统设置
              </h1>
              <p className={dsMode ? 'ds-page-sub' : 'text-sm text-muted-foreground'}>
                运行配置、用户权限和本地服务监控。
              </p>
            </div>
            <TabsList className="ios-segmented rounded-xl border border-border/70 bg-muted/40 p-1">
              <TabsTrigger value="runtime">运行配置</TabsTrigger>
              <TabsTrigger value="access">权限信息</TabsTrigger>
              <TabsTrigger value="audit">审计日志</TabsTrigger>
              <TabsTrigger value="license">授权许可</TabsTrigger>
              <TabsTrigger value="monitoring">服务监控</TabsTrigger>
            </TabsList>
          </div>

          <TabsContent value="runtime" className="ios-tabpanel mt-0 overflow-auto">
            <AdminRuntimePanel />
          </TabsContent>
          <TabsContent value="access" className="ios-tabpanel mt-0 overflow-auto">
            <AdminAccessPanel />
          </TabsContent>
          <TabsContent value="audit" className="ios-tabpanel mt-0 overflow-auto">
            <AdminAuditPanel />
          </TabsContent>
          <TabsContent value="license" className="ios-tabpanel mt-0 overflow-auto">
            <AdminLicensePanel />
          </TabsContent>
          <TabsContent value="monitoring" className="ios-tabpanel mt-0 overflow-auto">
            <AdminMonitoringPanel />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
