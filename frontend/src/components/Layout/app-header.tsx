// Copyright 2026 LexMask Contributors

import { useState } from 'react';
import { Globe, KeyRound, LogOut, Palette } from 'lucide-react';
import { useLocation } from 'react-router-dom';
import { useI18n, useT } from '@/i18n';
import { useServiceHealth } from '@/hooks/use-service-health';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { SidebarTrigger } from '@/components/ui/sidebar';
import { ChangePasswordDialog } from '@/components/ChangePasswordDialog';
import { useAuth } from '@/features/auth/auth-context';
import { useUiTheme } from '@/theme/ui-theme';

export function AppHeader() {
  const t = useT();
  const location = useLocation();
  const { locale, setLocale } = useI18n();
  const { theme, toggleTheme } = useUiTheme();
  const dsMode = theme === 'ds';
  const { health, checking } = useServiceHealth();
  const { status, logout } = useAuth();
  const [changePasswordOpen, setChangePasswordOpen] = useState(false);

  const { title, sub } = getPageHeader(location.pathname, t);
  const hasBusyService = Boolean(
    health && Object.values(health.services).some((service) => service.status === 'busy'),
  );
  const hasDegradedService = Boolean(
    health && Object.values(health.services).some((service) => service.status === 'degraded'),
  );
  const healthLabel = checking
    ? t('health.checking')
    : health?.all_online
      ? t('health.allOnline')
      : health
        ? hasBusyService
          ? t('health.someBusy')
          : hasDegradedService
            ? t('health.someDegraded')
            : t('health.someOffline')
        : t('health.backendDown');

  return (
    <header className="app-shell-header flex h-16 shrink-0 items-center justify-between border-b border-border/70 bg-background/95 px-4 backdrop-blur-2xl sm:px-6">
      <div className="flex min-h-[40px] min-w-0 flex-1 items-center gap-3">
        <SidebarTrigger className="lg:hidden" aria-label={t('layout.toggleSidebar')} />
        {/* Issue#355 ds 骨架：顶栏面包屑（定稿 .topbar .crumb；classic 不渲染） */}
        {dsMode && (
          <span className="ds-crumb hidden min-w-0 truncate lg:inline" aria-hidden={!dsMode}>
            {title}
          </span>
        )}
        <div className="flex min-w-0 flex-col justify-center lg:hidden" aria-label={title}>
          <div className="truncate text-lg font-semibold leading-tight tracking-tight text-foreground">
            {title}
          </div>
          {sub && <p className="mt-1 truncate text-xs leading-snug text-muted-foreground">{sub}</p>}
        </div>
      </div>

      <nav
        aria-label={t('layout.headerActions')}
        className="flex shrink-0 items-center gap-2 rounded-full border border-border/70 bg-[var(--surface-control)] px-2 py-1.5 shadow-[var(--shadow-control)]"
      >
        <div
          role="status"
          aria-label={healthLabel}
          title={healthLabel}
          className="grid size-8 place-items-center rounded-full"
          data-testid="health-indicator"
        >
          <span
            className={cn('h-2 w-2 rounded-full transition-colors', {
              'animate-pulse bg-muted-foreground/35': checking,
              'bg-[var(--success-foreground)]': !checking && health?.all_online,
              'bg-[var(--warning-foreground)]': !checking && health && !health.all_online,
              'bg-[var(--error-foreground)]': !checking && !health,
            })}
          />
        </div>

        <div className="mx-1 hidden h-4 w-px bg-border/60 sm:block" />

        <Button
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-2.5 text-xs text-muted-foreground hover:text-foreground"
          onClick={() => setLocale(locale === 'zh' ? 'en' : 'zh')}
          aria-label={t('layout.languageSwitch')}
          data-testid="lang-toggle"
        >
          <Globe className="size-3.5" />
          <span className="hidden sm:inline">
            {locale === 'zh' ? t('layout.switchToEnglish') : t('layout.switchToChinese')}
          </span>
        </Button>

        {/* Issue#355：风格切换（ds=新基线默认 / classic=升级前存量视觉），
            机制=根节点 data-theme 翻转 + localStorage 持久化，见 src/theme/ui-theme.ts。 */}
        <Button
          variant="ghost"
          size="sm"
          className="h-8 rounded-full px-2.5 text-xs text-muted-foreground hover:text-foreground"
          onClick={toggleTheme}
          aria-label={theme === 'ds' ? t('layout.switchToClassic') : t('layout.switchToModern')}
          aria-pressed={theme === 'ds'}
          data-testid="theme-toggle"
        >
          <Palette className="size-3.5" />
          <span className="hidden sm:inline">
            {theme === 'ds' ? t('layout.switchToClassic') : t('layout.switchToModern')}
          </span>
        </Button>

        {status?.auth_enabled && status.authenticated && (
          <>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 rounded-full px-2.5 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => setChangePasswordOpen(true)}
              aria-label={t('auth.changePassword')}
              data-testid="change-password-btn"
            >
              <KeyRound className="size-3.5" />
              <span className="hidden sm:inline">{t('auth.changePassword')}</span>
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-8 rounded-full px-2.5 text-xs text-muted-foreground hover:text-foreground"
              onClick={() => void logout()}
              aria-label={t('auth.logout')}
            >
              <LogOut className="size-3.5" />
              <span className="hidden sm:inline">{t('auth.logout')}</span>
            </Button>
          </>
        )}
      </nav>
      <ChangePasswordDialog
        open={changePasswordOpen}
        onClose={() => setChangePasswordOpen(false)}
      />
    </header>
  );
}

function getPageHeader(
  pathname: string,
  t: (key: string) => string,
): { title: string; sub?: string } {
  if (pathname === '/batch' || pathname.startsWith('/batch/'))
    return { title: t('page.batch.title'), sub: t('page.batch.sub') };
  if (pathname === '/structured')
    return { title: t('page.structured.title'), sub: t('page.structured.sub') };
  if (pathname === '/structured/files')
    return { title: t('nav.structured.files'), sub: t('nav.structured.files.sub') };
  if (pathname === '/structured/database')
    return { title: t('nav.structured.database'), sub: t('nav.structured.database.sub') };
  if (pathname === '/structured/datasets')
    return { title: t('nav.structured.datasets'), sub: t('nav.structured.datasets.sub') };
  if (pathname === '/structured/delivery')
    return { title: t('nav.structured.delivery'), sub: t('nav.structured.delivery.sub') };
  if (pathname === '/dicom') return { title: t('page.dicom.title'), sub: t('page.dicom.sub') };
  if (pathname === '/settings') return { title: t('page.config.title'), sub: t('page.config.sub') };
  if (pathname.startsWith('/jobs/'))
    return { title: t('page.jobDetail.title'), sub: t('page.jobDetail.sub') };
  if (pathname === '/jobs') return { title: t('page.jobs.title'), sub: t('page.jobs.sub') };

  const map: Record<string, { title: string; sub?: string }> = {
    '/single': { title: t('playground.title'), sub: t('page.playground.sub') },
    '/playground': { title: t('playground.title'), sub: t('page.playground.sub') },
    '/model-settings/text': { title: t('page.textModel.title'), sub: t('page.textModel.sub') },
    '/model-settings/vision': {
      title: t('page.visionModel.title'),
      sub: t('page.visionModel.sub'),
    },
  };

  return map[pathname] || { title: t('nav.playground') };
}
