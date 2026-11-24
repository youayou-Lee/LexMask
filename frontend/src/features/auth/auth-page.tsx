// Copyright 2026 LexMask Contributors

import { useEffect, useMemo, useState, type FormEvent } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { BRAND, brandName, brandTagline } from '@/config/brand';
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useT } from '@/i18n';
import { useUiTheme } from '@/theme/ui-theme';
import { resolveAuthMode, resolveAuthNext, type AuthMode } from './auth-routing';
import { useAuth } from './auth-context';

export function AuthPage() {
  const t = useT();
  const location = useLocation();
  const navigate = useNavigate();
  const { status, login, setup, register } = useAuth();
  // Issue#355：登录页双轨——ds 下为「三栏海报」构图（品牌主张 / 居中玻璃登录卡 /
  // 脱敏文书叠卡，2026-11-25 视觉重设计）；classic 逐像素保持原绿渐变白卡。
  const dsMode = useUiTheme((s) => s.theme) === 'ds';
  const [mode, setMode] = useState<AuthMode>(() => resolveAuthMode(location.search));
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // License 到期/失效横幅（/license/status 是公开端点，登录前即可读）
  const [licenseNotice, setLicenseNotice] = useState<string | null>(null);
  useEffect(() => {
    fetch('/api/v1/license/status')
      .then(async (res) => {
        if (!res.ok) return;
        const s = (await res.json()) as { state?: string; days_left?: number | null };
        const key =
          s.state === 'expiring_soon'
            ? 'auth.license.expiringSoon'
            : s.state === 'grace_readonly'
              ? 'auth.license.graceReadonly'
              : s.state === 'blocked' || s.state === 'invalid'
                ? 'auth.license.blocked'
                : null;
        if (key) setLicenseNotice(t(key).replace('{days}', String(s.days_left ?? '')));
      })
      .catch(() => {
        /* 拉不到状态就不显示横幅 */
      });
  }, [t]);

  const needsSetup = status?.password_set === false;
  const isRegister = !needsSetup && mode === 'register';
  const next = useMemo(() => resolveAuthNext(location.search), [location.search]);
  const lede = t('auth.ds.lede').replace('{brand}', brandName(t));

  function switchMode(nextMode: AuthMode) {
    setError(null);
    setConfirmPassword('');
    setMode(nextMode);
    const params = new URLSearchParams(location.search);
    if (nextMode === 'register') {
      params.set('mode', 'register');
    } else {
      params.delete('mode');
    }
    const search = params.toString();
    navigate(`${location.pathname}${search ? `?${search}` : ''}`, { replace: true });
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError(null);

    if ((needsSetup || isRegister) && password !== confirmPassword) {
      setError(t('auth.error.passwordMismatch'));
      return;
    }

    setSubmitting(true);
    try {
      if (needsSetup) {
        await setup(username, password);
      } else if (isRegister) {
        await register(username, password);
      } else {
        await login(username, password);
      }
      navigate(isRegister ? '/single' : next, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : t('auth.error.generic'));
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className={
        dsMode
          ? 'ds-auth-root relative flex min-h-dvh flex-col overflow-hidden'
          : 'relative flex min-h-dvh items-center justify-center overflow-hidden bg-[radial-gradient(circle_at_top,_rgba(16,185,129,0.18),_transparent_42%),linear-gradient(160deg,#f4f7f4_0%,#eef2ef_45%,#dde6df_100%)] px-6 py-10'
      }
      data-testid={dsMode ? 'auth-page-ds' : undefined}
    >
      {dsMode ? (
        <>
          {/* Issue#355：登录页背景移植比赛主视觉（design-proto/competition-media/poster.html）——
              登录卡严格居中（用户裁定 2026-11-20「肯定居中」），左侧品牌主张 + 右侧脱敏文书叠卡
              把版面撑满（用户裁定 2026-11-25「其他内容太少显得很空」）。
              文书为纯 CSS 示意，无任何真实案卷数据。 */}
          <div className="ds-auth-poster-grid pointer-events-none absolute inset-0" aria-hidden="true" />
          <div className="ds-auth-poster-halo pointer-events-none absolute" aria-hidden="true" />
          <div className="ds-auth-halo-card pointer-events-none absolute" aria-hidden="true" />

          <header className="ds-auth-topbar">
            <div className="ds-auth-lockup">
              <img src={BRAND.logoUrl} alt="" className="ds-auth-lockup-logo" />
              <span className="ds-auth-lockup-name">{brandName(t)}</span>
              <span className="ds-auth-lockup-rule" aria-hidden="true" />
              <span className="ds-auth-lockup-desc">{brandTagline(t)}</span>
            </div>
            <div className="ds-auth-topbar-right">
              <span className="ds-auth-privacy">
                <i aria-hidden="true" />
                {t('auth.ds.privacy')}
              </span>
              {/* 右上角一键回 classic（登录前也可切；与主壳切换键同一机制） */}
              <button
                type="button"
                onClick={() => useUiTheme.getState().toggleTheme()}
                className="ds-auth-switch"
                aria-label={t('layout.switchToClassic')}
              >
                {t('layout.switchToClassic')}
              </button>
            </div>
          </header>

          <main className="ds-auth-stage">
            <section className="ds-auth-claim">
              <div className="ds-auth-kicker">{t('auth.ds.kicker')}</div>
              <div className="ds-auth-claim-title">
                <span>{t('auth.ds.claimA')}</span>
                <span>{t('auth.ds.claimB')}</span>
              </div>
              <div className="ds-auth-claim-en">{t('auth.ds.enLine')}</div>
              <p className="ds-auth-claim-lede">{lede}</p>
              <ul className="ds-auth-facts">
                <li>
                  <span className="ds-auth-fact-n">01</span>
                  <span className="ds-auth-fact-k">{t('auth.ds.factCompliance')}</span>
                  <span className="ds-auth-fact-v">{t('auth.ds.factComplianceNote')}</span>
                </li>
                <li>
                  <span className="ds-auth-fact-n">02</span>
                  <span className="ds-auth-fact-k">{t('auth.ds.factUsable')}</span>
                  <span className="ds-auth-fact-v">{t('auth.ds.factUsableNote')}</span>
                </li>
                <li>
                  <span className="ds-auth-fact-n">03</span>
                  <span className="ds-auth-fact-k">{t('auth.ds.factAuditable')}</span>
                  <span className="ds-auth-fact-v">{t('auth.ds.factAuditableNote')}</span>
                </li>
              </ul>
            </section>

            <div className="ds-auth-card-wrap">
              <Card className="ds-auth-card relative w-full">{renderCardBody()}</Card>
            </div>

            <section className="ds-auth-docs" aria-hidden="true">
              <div className="ds-auth-sheet" />
              <div className="ds-auth-poster-doc ds-auth-doc-back">
                <div className="ds-auth-doc-head">
                  <span className="ds-auth-doc-title">执行裁定书</span>
                  <span className="ds-auth-doc-chip">已脱敏</span>
                </div>
                <div className="ds-auth-doc-rule" />
                <div className="ds-auth-doc-lines">
                  <i style={{ width: '90%' }} />
                  <i style={{ width: '97%' }}><b style={{ left: '0%', width: '19%' }} /></i>
                  <i style={{ width: '86%' }} />
                  <i style={{ width: '94%' }}>
                    <b style={{ left: '14%', width: '15%' }} />
                    <b style={{ left: '52%', width: '12%' }} />
                  </i>
                  <i style={{ width: '91%' }} />
                  <i style={{ width: '88%' }}><b style={{ left: '34%', width: '16%' }} /></i>
                  <i style={{ width: '95%' }} />
                  <i style={{ width: '80%' }} />
                  <i style={{ width: '93%' }}><b style={{ left: '7%', width: '13%' }} /></i>
                  <i style={{ width: '87%' }} />
                  <i style={{ width: '74%' }} />
                </div>
                <div className="ds-auth-doc-foot">
                  <span>第 11 页 / 共 36 页</span>
                  <span>MASK · 涂黑 / 化名</span>
                </div>
              </div>
              <div className="ds-auth-poster-doc ds-auth-doc-front">
                <div className="ds-auth-doc-head">
                  <span className="ds-auth-doc-title">民事判决书</span>
                  <span className="ds-auth-doc-chip">已脱敏</span>
                </div>
                <div className="ds-auth-doc-rule" />
                <div className="ds-auth-doc-lines">
                  <i style={{ width: '88%' }} />
                  <i style={{ width: '96%' }}>
                    <b style={{ left: '0%', width: '18%' }} />
                    <b style={{ left: '35%', width: '10%' }} />
                  </i>
                  <i style={{ width: '92%' }} />
                  <i style={{ width: '99%' }}><b style={{ left: '46%', width: '23%' }} /></i>
                  <i style={{ width: '84%' }} />
                  <i style={{ width: '95%' }}>
                    <b style={{ left: '6%', width: '15%' }} />
                    <b style={{ left: '53%', width: '8%' }} />
                  </i>
                  <i style={{ width: '90%' }} />
                  <i style={{ width: '97%' }}><b style={{ left: '26%', width: '21%' }} /></i>
                  <i style={{ width: '86%' }} />
                  <i style={{ width: '93%' }}>
                    <b style={{ left: '0%', width: '11%' }} />
                    <b style={{ left: '41%', width: '17%' }} />
                  </i>
                  <i style={{ width: '89%' }} />
                  <i style={{ width: '98%' }}><b style={{ left: '37%', width: '13%' }} /></i>
                  <i style={{ width: '91%' }} />
                  <i style={{ width: '64%' }} />
                </div>
                <div className="ds-auth-doc-foot">
                  <span>第 03 页 / 共 148 页</span>
                  <span>MASK · 涂黑 / 化名</span>
                </div>
              </div>
              <div className="ds-auth-doc-cap">示意 · 非真实案卷数据</div>
            </section>
          </main>

          <footer className="ds-auth-footer">
            <div className="ds-auth-footer-left">
              <b>{brandName(t)}</b>
              <span>{brandTagline(t)}</span>
            </div>
            <div className="ds-auth-footer-right">
              <span>{t('auth.ds.footerValues')}</span>
              <span className="ds-auth-footer-dot">{t('auth.ds.footerLocal')}</span>
              <span>© 2026</span>
            </div>
          </footer>
        </>
      ) : (
        <div className="flex min-h-dvh items-center justify-center">
          <div className="pointer-events-none absolute inset-0 bg-[linear-gradient(135deg,rgba(15,23,42,0.04)_0%,transparent_38%,rgba(15,23,42,0.03)_100%)]" />
          <Card className="relative z-10 w-full max-w-md border-border/70 bg-background/92">
            {renderCardBody()}
          </Card>
        </div>
      )}
    </div>
  );

  /** 表单主体（双轨共用，避免两份 JSX 漂移） */
  function renderCardBody() {
    return (
      <>
        {licenseNotice && (
          <div
            className="rounded-t-[var(--radius-xl)] border-b border-[var(--warning-border)] bg-[var(--warning-surface)] px-6 py-2 text-xs text-[var(--warning-foreground)]"
            data-testid="license-banner"
          >
            {licenseNotice}
          </div>
        )}
        <CardHeader className={!dsMode ? 'space-y-4' : 'ds-auth-head'}>
          <div className="flex items-center gap-3">
            <img
              src={BRAND.logoUrl}
              alt=""
              className={dsMode ? 'ds-auth-logo' : 'size-12 rounded-2xl shadow-[var(--shadow-md)]'}
            />
            <div className="min-w-0">
              <div className="truncate text-sm font-semibold tracking-tight">{brandName(t)}</div>
              <div className="truncate text-xs text-muted-foreground">{brandTagline(t)}</div>
            </div>
          </div>
          <div className={!dsMode ? 'space-y-2' : 'ds-auth-titleblock'}>
            <CardTitle className={dsMode ? 'ds-auth-title' : 'text-2xl tracking-[-0.04em]'}>
              {needsSetup
                ? t('auth.setup.title')
                : isRegister
                  ? t('auth.register.title')
                  : t('auth.login.title')}
            </CardTitle>
            <CardDescription className={dsMode ? 'ds-auth-desc' : undefined}>
              {needsSetup
                ? t('auth.setup.description')
                : isRegister
                  ? t('auth.register.description')
                  : t('auth.login.description')}
            </CardDescription>
          </div>
        </CardHeader>
        <CardContent className={dsMode ? 'ds-auth-content' : undefined}>
          <form
            className={!dsMode ? 'space-y-5' : 'ds-auth-form'}
            onSubmit={(event) => void handleSubmit(event)}
          >
            {error && (
              <Alert variant="destructive">
                <AlertTitle>{t('auth.error.title')}</AlertTitle>
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}

            <div className="space-y-2">
              <Label htmlFor="auth-username">{t('auth.username')}</Label>
              <Input
                id="auth-username"
                type="text"
                autoComplete="username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                placeholder={t('auth.username.placeholder')}
                required
                className={dsMode ? 'ds-auth-input' : undefined}
              />
            </div>

            <div className="space-y-2">
              <Label htmlFor="auth-password">{t('auth.password')}</Label>
              <Input
                id="auth-password"
                type="password"
                autoComplete={needsSetup || isRegister ? 'new-password' : 'current-password'}
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                placeholder={t('auth.password.placeholder')}
                required
                className={dsMode ? 'ds-auth-input' : undefined}
              />
            </div>

            {(needsSetup || isRegister) && (
              <div className="space-y-2">
                <Label htmlFor="auth-confirm-password">{t('auth.confirmPassword')}</Label>
                <Input
                  id="auth-confirm-password"
                  type="password"
                  autoComplete="new-password"
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  placeholder={t('auth.confirmPassword.placeholder')}
                  required
                  className={dsMode ? 'ds-auth-input' : undefined}
                />
              </div>
            )}

            <Button
              className={dsMode ? 'ds-auth-submit w-full' : 'w-full'}
              type="submit"
              disabled={submitting}
            >
              {submitting
                ? t('auth.submitting')
                : needsSetup
                  ? t('auth.setup.submit')
                  : isRegister
                    ? t('auth.register.submit')
                    : t('auth.login.submit')}
            </Button>

            {needsSetup ? (
              <p className={dsMode ? 'ds-auth-hint' : 'text-sm leading-6 text-muted-foreground'}>
                {t('auth.setup.hint')}
              </p>
            ) : (
              <div
                className={
                  dsMode ? 'ds-auth-hintblock' : 'space-y-3 text-sm leading-6 text-muted-foreground'
                }
              >
                <p>{isRegister ? t('auth.register.hint') : t('auth.login.hint')}</p>
                <button
                  type="button"
                  className="text-sm font-medium text-foreground underline-offset-4 hover:underline"
                  onClick={() => switchMode(isRegister ? 'login' : 'register')}
                >
                  {isRegister ? t('auth.register.switchToLogin') : t('auth.login.switchToRegister')}
                </button>
              </div>
            )}
          </form>
          {dsMode && <div className="ds-auth-cardfoot">{t('auth.ds.cardFoot')}</div>}
        </CardContent>
      </>
    );
  }
}

export default AuthPage;