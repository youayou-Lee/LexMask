// Copyright 2026 LexMask Contributors

import { useEffect, useRef, useState } from 'react';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { authFetch } from '@/services/api-client';
import { showToast } from '@/components/Toast';
import { localizeErrorMessage } from '@/utils/localizeError';
import type { EncryptedPdfPrompt } from '../hooks/use-playground-file';

interface Props {
  prompt: EncryptedPdfPrompt | null;
  /** 密码解密成功，续跑 parse → 自动识别 */
  onDecrypted: (fileId: string, filename: string) => void;
  onCancel: () => void;
}

/** Issue #30：加密 PDF 的密码输入框。密码仅本次提交给 decrypt 端点，不落任何状态。 */
export function EncryptedPdfDialog({ prompt, onDecrypted, onCancel }: Props) {
  if (!prompt) return null;
  // key 随文件变化重挂载，密码/提交态天然归零，无需 effect 重置
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open) onCancel();
      }}
    >
      <DialogContent className="max-w-md p-6 [&>button]:hidden">
        <EncryptedPdfForm key={prompt.fileId} prompt={prompt} onDecrypted={onDecrypted} onCancel={onCancel} />
      </DialogContent>
    </Dialog>
  );
}

function EncryptedPdfForm({
  prompt,
  onDecrypted,
  onCancel,
}: {
  prompt: EncryptedPdfPrompt;
  onDecrypted: (fileId: string, filename: string) => void;
  onCancel: () => void;
}) {
  const t = useT();
  const [password, setPassword] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    // Radix 挂载动画后再聚焦，避免被 content 的初始渲染顶掉
    const timer = setTimeout(() => inputRef.current?.focus(), 50);
    return () => clearTimeout(timer);
  }, []);

  const submit = async () => {
    if (submitting || !password) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await authFetch(`/api/v1/files/${prompt.fileId}/decrypt`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password }),
      });
      if (res.ok) {
        onDecrypted(prompt.fileId, prompt.filename);
        return;
      }
      const data = (await res.json().catch(() => null)) as { error_code?: string } | null;
      if (data?.error_code === 'PDF_WRONG_PASSWORD') {
        setError(t('common.pdfWrongPassword'));
      } else {
        showToast(
          localizeErrorMessage(
            { response: { status: res.status, data } },
            'playground.processFailed',
          ),
          'error',
        );
        setSubmitting(false);
      }
    } catch {
      showToast(t('common.networkError'), 'error');
      setSubmitting(false);
    }
  };

  return (
    <>
      <DialogHeader className="flex flex-col gap-2 text-left">
        <DialogTitle className="text-base font-semibold tracking-[-0.02em]">
          {t('playground.encryptedPdf.title')}
        </DialogTitle>
        <DialogDescription className="whitespace-pre-line text-sm text-muted-foreground">
          {t('playground.encryptedPdf.description').replace('{filename}', prompt.filename)}
        </DialogDescription>
      </DialogHeader>
      <form
        className="flex flex-col gap-2 pt-1"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <Input
          ref={inputRef}
          type="password"
          value={password}
          onChange={(e) => {
            setPassword(e.target.value);
            setError(null);
          }}
          placeholder={t('playground.encryptedPdf.placeholder')}
          autoComplete="off"
          disabled={submitting}
        />
        {error && <p className="text-sm text-destructive">{error}</p>}
        <DialogFooter className="gap-2 pt-1 sm:justify-end">
          <Button type="button" variant="outline" onClick={onCancel} disabled={submitting}>
            {t('common.cancel')}
          </Button>
          <Button type="submit" disabled={submitting || !password}>
            {submitting
              ? t('playground.encryptedPdf.decrypting')
              : t('playground.encryptedPdf.submit')}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
