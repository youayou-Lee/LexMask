// Copyright 2026 LexMask Contributors

import { AlertCircle, CheckCircle2, Info } from 'lucide-react';
import { Toaster, toast } from 'sonner';
import { t } from '@/i18n';

type ToastType = 'success' | 'error' | 'info';

const TOAST_ICON: Record<ToastType, typeof CheckCircle2> = {
  success: CheckCircle2,
  error: AlertCircle,
  info: Info,
};

const TOAST_CLASS: Record<ToastType, string> = {
  success: 'border-[var(--success-border)] bg-[var(--surface-overlay)] text-foreground',
  error: 'border-[var(--error-border)] bg-[var(--surface-overlay)] text-foreground',
  info: 'border-[var(--info-border)] bg-[var(--surface-overlay)] text-foreground',
};

const TOAST_ICON_CLASS: Record<ToastType, string> = {
  success: 'text-[var(--success-foreground)]',
  error: 'text-[var(--error-foreground)]',
  info: 'text-[var(--info-foreground)]',
};

function normalizeToastMessage(message: unknown): string {
  if (typeof message === 'string' && message.trim()) return message.trim();
  if (message instanceof Error && message.message.trim()) return message.message.trim();
  if (message && typeof message === 'object') {
    const detail = (message as { detail?: unknown }).detail;
    if (typeof detail === 'string' && detail.trim()) return detail.trim();
    const nestedMessage = (message as { message?: unknown }).message;
    if (typeof nestedMessage === 'string' && nestedMessage.trim()) return nestedMessage.trim();
  }
  return t('toast.fallbackError');
}

export function showToast(
  message: unknown,
  type: ToastType = 'info',
  // Issue#194 终审 D（AC2）：通知类 toast 可点击直达。sonner 2.0.7 的 toast 本体
  // 无原生 onClick（ExternalToast 不收该键，传了既类型报错又运行时静默丢弃），
  // 故用其受支持的 action 按钮承担点击直达；旧调用不传即无按钮、外观不变。
  options?: { onClick?: () => void },
) {
  const Icon = TOAST_ICON[type];

  toast(normalizeToastMessage(message), {
    duration: 2400,
    ...(options?.onClick
      ? { action: { label: t('toast.open'), onClick: options.onClick } }
      : {}),
    className: [
      'rounded-2xl border px-4 py-3 shadow-[var(--shadow-lg)] backdrop-blur-xl',
      TOAST_CLASS[type],
    ].join(' '),
    descriptionClassName: 'text-sm text-muted-foreground',
    icon: <Icon className={TOAST_ICON_CLASS[type]} />,
  });
}

export function ToastContainer() {
  return (
    <Toaster
      position="top-center"
      theme="light"
      gap={10}
      closeButton
      richColors={false}
      toastOptions={{
        classNames: {
          toast:
            'group rounded-2xl border border-border bg-[var(--surface-overlay)] text-foreground shadow-[var(--shadow-lg)] backdrop-blur-xl',
          title: 'text-sm font-medium text-foreground',
          closeButton:
            'border-border bg-[var(--surface-control)] text-muted-foreground hover:bg-accent hover:text-foreground',
        },
      }}
    />
  );
}
