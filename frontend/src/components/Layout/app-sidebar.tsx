// Copyright 2026 LexMask Contributors

import { NavLink, useLocation } from 'react-router-dom';
import {
  BookOpen,
  Database,
  Eraser,
  FileSpreadsheet,
  LayoutDashboard,
  PackageCheck,
  RefreshCw,
  ScanLine,
  Server,
  Sparkles,
  TableProperties,
  Undo2,
} from 'lucide-react';
import { useT } from '@/i18n';
import { BRAND, brandName, brandTagline } from '@/config/brand';
import { cn } from '@/lib/utils';
import { useAuth } from '@/features/auth/auth-context';
import { useSingleTask } from '@/features/single-task/single-task-context';
import { useServiceHealth } from '@/hooks/use-service-health';
import {
  PlayIcon,
  BatchIcon,
  JobsCenterIcon,
  ModelIcon,
  RulesIcon,
} from '@/components/shared/nav-icons';
import { isTerminalPhase } from '@/features/single-task/phase';
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarHeader,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  SidebarSeparator,
} from '@/components/ui/sidebar';

// 暂时下线的处理流程入口：整条从侧边栏隐藏（路由与页面保留可达），恢复开放时从此处删除即可。
const HIDDEN_NAV_PATHS: ReadonlySet<string> = new Set(['/structured', '/dicom']);

export function filterHiddenNavItems<T extends { path: string }>(items: T[]): T[] {
  return items.filter((item) => !HIDDEN_NAV_PATHS.has(item.path));
}

interface NavItem {
  path: string;
  label: string;
  sublabel?: string;
  icon: React.FC<{ className?: string }>;
  end?: boolean;
  children?: NavItem[];
  /** 可选计数角标（Issue#262：/jobs 挂非终态单文件任务数）；0/缺省不渲染。 */
  badgeCount?: number;
}

export function AppSidebar() {
  const t = useT();
  const location = useLocation();
  const { status } = useAuth();
  const { checking, refresh } = useServiceHealth();
  const { tasks } = useSingleTask();
  const isAdmin = Boolean(status?.is_super_admin);
  // Issue#262：角标只计进行中/待复核的任务——done/failed 是终态，不应把用户拽回任务中心。
  const nonTerminalCount = tasks.filter((task) => !isTerminalPhase(task.phase)).length;

  const workflowNavItems: NavItem[] = [
    {
      path: '/single',
      label: t('nav.playground'),
      sublabel: t('nav.playground.sub'),
      icon: PlayIcon,
      end: true,
    },
    { path: '/batch', label: t('nav.batch'), sublabel: t('nav.batch.sub'), icon: BatchIcon },
    {
      path: '/agent-md',
      label: t('nav.agentMd'),
      sublabel: t('nav.agentMd.sub'),
      icon: Sparkles,
      end: true,
    },
    {
      path: '/structured',
      label: t('nav.structured'),
      sublabel: t('nav.structured.sub'),
      icon: Database,
      children: [
        {
          path: '/structured/files',
          label: t('nav.structured.files'),
          sublabel: t('nav.structured.files.sub'),
          icon: FileSpreadsheet,
        },
        {
          path: '/structured/database',
          label: t('nav.structured.database'),
          sublabel: t('nav.structured.database.sub'),
          icon: Server,
        },
        {
          path: '/structured/datasets',
          label: t('nav.structured.datasets'),
          sublabel: t('nav.structured.datasets.sub'),
          icon: TableProperties,
        },
        {
          path: '/structured/delivery',
          label: t('nav.structured.delivery'),
          sublabel: t('nav.structured.delivery.sub'),
          icon: PackageCheck,
        },
      ],
    },
    {
      path: '/dicom',
      label: t('nav.dicom'),
      sublabel: t('nav.dicom.sub'),
      icon: ScanLine,
    },
    {
      path: '/restore',
      label: t('nav.restore'),
      sublabel: t('nav.restore.sub'),
      icon: Undo2,
    },
    {
      path: '/watermark-tool',
      label: t('nav.watermarkTool'),
      sublabel: t('nav.watermarkTool.sub'),
      icon: Eraser,
    },
    {
      path: '/jobs',
      label: t('nav.jobs'),
      sublabel: t('nav.jobs.sub'),
      icon: JobsCenterIcon,
      // Issue#262：非终态计数（进行中 + 待复核），零新请求——复用账本 tasks。
      badgeCount: nonTerminalCount,
    },
    {
      // Issue#348：文档中心入口——放在工作流组末位，与「任务中心」同族的一级导航。
      // 不带 end：`/docs` 只是重定向入口（→ /docs/:slug），等值匹配会让菜单项
      // 在文档中心内永不高亮（同族先例：/batch 有子路由也不带 end）。
      path: '/docs',
      label: t('nav.docs'),
      sublabel: t('nav.docs.sub'),
      icon: BookOpen,
    },
  ];

  const visibleWorkflowNavItems = filterHiddenNavItems(workflowNavItems);

  // Issue#262：法律默认档收敛——识别项/配置清单/词库收进 /settings 单入口
  // （高级设置三页签：识别项 / 配置清单 / 词库）。旧 /settings/redaction、
  // /settings/word-pools 路由只作深链兜底，经 router 的 SettingsTabRedirect
  // 重定向到 /settings?tab=…；页签面板 kicker 复用 nav.redactionList.sub 等既有键。
  const configNavItems: NavItem[] = [
    {
      path: '/settings',
      label: t('nav.advanced'),
      sublabel: t('nav.advanced.sub'),
      icon: RulesIcon,
      end: true,
    },
    ...(isAdmin
      ? [
          {
            path: '/console',
            label: t('nav.console'),
            sublabel: t('nav.console.sub'),
            icon: LayoutDashboard,
          },
          {
            path: '/settings/system',
            label: t('nav.systemSettings'),
            sublabel: t('nav.systemSettings.sub'),
            icon: ModelIcon,
          },
        ]
      : []),
  ];

  return (
    <Sidebar collapsible="offcanvas" variant="inset">
      <SidebarHeader className="h-16 flex-row items-center gap-3 border-b border-sidebar-border px-4">
        <img
          src={BRAND.logoUrl}
          alt=""
          className="size-10 shrink-0 rounded-xl shadow-[var(--shadow-sm)]"
        />
        <div className="min-w-0">
          <span className="block truncate text-sm font-semibold leading-tight tracking-tight text-sidebar-foreground">
            {brandName(t)}
          </span>
          <p className="mt-0.5 truncate text-xs text-sidebar-foreground/55">{brandTagline(t)}</p>
        </div>
      </SidebarHeader>

      <SidebarContent className="gap-0 px-2 py-1.5">
        <nav aria-label={t('layout.navLabel')}>
          <SidebarGroup className="px-2 py-0">
            <SidebarGroupLabel className="h-6 px-2 text-xs font-semibold uppercase tracking-wide text-sidebar-foreground/50">
              {t('nav.group.workflow')}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu className="gap-0.5">
                {visibleWorkflowNavItems.map((item) => (
                  <SidebarNavItem key={item.path} item={item} pathname={location.pathname} />
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>

          <SidebarSeparator className="my-1 bg-sidebar-border opacity-100" />

          <SidebarGroup className="px-2 py-0">
            <SidebarGroupLabel className="h-6 px-2 text-xs font-semibold uppercase tracking-wide text-sidebar-foreground/50">
              {t('nav.group.config')}
            </SidebarGroupLabel>
            <SidebarGroupContent>
              <SidebarMenu className="gap-0.5">
                {configNavItems.map((item) => (
                  <SidebarNavItem key={item.path} item={item} pathname={location.pathname} />
                ))}
              </SidebarMenu>
            </SidebarGroupContent>
          </SidebarGroup>
        </nav>
      </SidebarContent>

      <SidebarFooter className="shrink-0 p-2">
        <SidebarServiceStatus checking={checking} onRefresh={refresh} />
      </SidebarFooter>
    </Sidebar>
  );
}

function SidebarNavItem({ item, pathname }: { item: NavItem; pathname: string }) {
  const active = isNavActive(item, pathname);
  const showChildren = active && item.children && item.children.length > 0;
  const pathKey = item.path.replace(/\//g, '-').replace(/^-/, '') || 'start';
  const showBadge = typeof item.badgeCount === 'number' && item.badgeCount > 0;

  return (
    <SidebarMenuItem>
      <SidebarMenuButton
        asChild
        isActive={active}
        tooltip={item.label}
        className={cn(
          'min-h-12 rounded-2xl border border-transparent px-3 py-2.5 transition-all duration-150',
          active &&
            'border-sidebar-border bg-sidebar-accent font-medium text-sidebar-foreground shadow-[var(--shadow-control)]',
        )}
      >
        <NavLink
          to={item.path}
          end={item.end}
          aria-label={item.sublabel ? `${item.label} - ${item.sublabel}` : item.label}
          data-testid={`nav-${pathKey}`}
        >
          <item.icon className="size-4 shrink-0 opacity-70" />
          {item.sublabel ? (
            <span className="flex min-w-0 flex-col gap-0.5 py-0.5 leading-snug">
              <span className="truncate text-xs font-medium leading-tight">{item.label}</span>
              <span className="truncate text-[11px] font-normal leading-tight opacity-45">
                {item.sublabel}
              </span>
            </span>
          ) : (
            <span className="truncate text-xs font-medium leading-tight">{item.label}</span>
          )}
          {showBadge ? (
            // 计数角标：pill 抄 SidebarServiceStatus 的徽章 span（rounded-full px-1
            // py-0.5 text-[10px]），色调用品牌 success 面（与侧边栏 ring 同族的强调色）。
            <span
              className="ml-auto shrink-0 rounded-full bg-[var(--success-surface)] px-1 py-0.5 text-[10px] font-semibold leading-none text-[var(--success-foreground)] tabular-nums"
              data-testid={`nav-badge-${pathKey}`}
            >
              {item.badgeCount}
            </span>
          ) : null}
        </NavLink>
      </SidebarMenuButton>
      {showChildren ? (
        <div className="mt-0.5 grid gap-0.5 pl-5">
          {item.children?.map((child) => (
            <SidebarSubNavItem key={child.path} item={child} pathname={pathname} />
          ))}
        </div>
      ) : null}
    </SidebarMenuItem>
  );
}

function SidebarSubNavItem({ item, pathname }: { item: NavItem; pathname: string }) {
  const active = isNavActive(item, pathname);
  return (
    <NavLink
      to={item.path}
      className={cn(
        'grid min-h-9 grid-cols-[1rem_minmax(0,1fr)] items-center gap-2 rounded-xl border border-transparent px-2.5 py-1.5 text-sidebar-foreground/70 transition-all duration-150 hover:bg-sidebar-accent hover:text-sidebar-foreground',
        active &&
          'border-sidebar-border bg-sidebar-accent text-sidebar-foreground shadow-[var(--shadow-control)]',
      )}
      aria-label={item.sublabel ? `${item.label} - ${item.sublabel}` : item.label}
      data-testid={`nav-${item.path.replace(/\//g, '-').replace(/^-/, '')}`}
    >
      <item.icon className="size-3.5 opacity-65" />
      <span className="min-w-0">
        <span className="block truncate text-xs font-medium">{item.label}</span>
        {item.sublabel ? <span className="sr-only">{item.sublabel}</span> : null}
      </span>
    </NavLink>
  );
}

function isNavActive(item: NavItem, pathname: string): boolean {
  if (item.path === '/settings') {
    return pathname === '/settings' || pathname.startsWith('/model-settings');
  }
  if (item.end) return pathname === item.path;
  return pathname === item.path || pathname.startsWith(item.path + '/');
}

function SidebarServiceStatus({
  checking,
  onRefresh,
}: {
  checking: boolean;
  onRefresh: () => void;
}) {
  const t = useT();

  // Issue#351 P2-D1（v2 方案）：SidebarFooter 常驻卡降级为「标题+刷新」一行。
  // - 明细行/GPU 显存/版本行移出：对终端律师用户零决策价值（窄栏还挤成竖排断字）；
  //   明细的正规归宿=系统设置→服务监控（管理员），版本行随之挪入监控页。
  // - 总体状态点删除：AppHeader 已有 health-indicator（同一信息只出现一次）。
  // - 保留刷新按钮：服务异常时用户想重试的直觉入口。
  return (
    <section
      className="flex min-h-0 min-w-0 items-center justify-between gap-2 overflow-hidden rounded-xl border border-sidebar-border bg-sidebar-accent px-2.5 py-2 text-sidebar-foreground shadow-[var(--shadow-sm)]"
      aria-label={t('health.sidebar.title')}
      data-testid="sidebar-service-status"
    >
      <p className="min-w-0 truncate text-sm font-semibold">{t('health.sidebar.title')}</p>
      <button
        type="button"
        onClick={onRefresh}
        className="grid size-6 shrink-0 place-items-center rounded-full text-sidebar-foreground/60 transition hover:bg-sidebar-primary hover:text-sidebar-foreground"
        title={t('health.refreshTitle')}
        aria-label={t('health.refreshTitle')}
        data-testid="health-refresh"
      >
        <RefreshCw className={cn('size-3.5', checking && 'animate-spin')} />
      </button>
    </section>
  );
}
