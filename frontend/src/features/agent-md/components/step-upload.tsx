// Copyright 2026 LexMask Contributors

import { useCallback, useState } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { useT } from '@/i18n';
import { authFetch } from '@/services/api-client';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

/**
 * 读取 /agent-md/upload 的错误信封（AppError：{error_code, message, detail, request_id}）。
 * 与 playground 的 readErrorEnvelope 同款：响应体非 JSON 时回退 null，由调用方走 i18n 文案。
 * 加密 PDF 复用 Issue#30 链路的两个 code：409 PDF_ENCRYPTED_NEEDS_PASSWORD / PDF_WRONG_PASSWORD。
 */
async function readUploadEnvelope(
  res: Response,
): Promise<{ code: string | null; message: string | null }> {
  try {
    const data = (await res.json()) as {
      error_code?: unknown;
      message?: unknown;
      detail?: unknown;
    };
    const code = typeof data.error_code === 'string' && data.error_code ? data.error_code : null;
    const detail = data.message ?? data.detail;
    const message = typeof detail === 'string' && detail.trim() ? detail.trim() : null;
    return { code, message };
  } catch {
    return { code: null, message: null };
  }
}

interface StepUploadProps {
  onUploaded: (taskId: string) => void;
}

/**
 * 四步动线第一步：PDF 投递 + 加密 PDF 条件密码分支（Issue#75）。
 * 上传失败只走本步内的 issue 框展示（不喂页面横幅，评审 R1 去重——横幅留给后续步骤的错误）。
 */
export function StepUpload({ onUploaded }: StepUploadProps) {
  const t = useT();
  // file 留在组件态以便带密码重传；密码只存这里，不打日志、不进任何全局状态
  const [file, setFile] = useState<File | null>(null);
  const [needPassword, setNeedPassword] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);

  const upload = useCallback(
    async (target: File, pwd?: string) => {
      setBusy(true);
      setIssue(null);
      try {
        const fd = new FormData();
        fd.append('file', target);
        if (pwd) fd.append('password', pwd);
        const res = await authFetch('/api/v1/agent-md/upload', { method: 'POST', body: fd });
        if (!res.ok) {
          const { code, message } = await readUploadEnvelope(res);
          if (code === 'PDF_ENCRYPTED_NEEDS_PASSWORD') {
            setNeedPassword(true);
            setIssue(t('agentMd.needPassword'));
            return;
          }
          if (code === 'PDF_WRONG_PASSWORD') {
            setNeedPassword(true);
            setIssue(t('agentMd.wrongPassword'));
            return;
          }
          throw new Error(message || t('agentMd.uploadFailed'));
        }
        const data = (await res.json()) as { task_id: string };
        setPassword('');
        setNeedPassword(false);
        onUploaded(data.task_id);
      } catch (err) {
        const message = err instanceof Error && err.message ? err.message : t('agentMd.uploadFailed');
        setIssue(message);
      } finally {
        setBusy(false);
      }
    },
    [onUploaded, t],
  );

  const onDrop = useCallback(
    (accepted: File[]) => {
      if (accepted.length === 0) return;
      const picked = accepted[0];
      setFile(picked);
      setNeedPassword(false);
      setPassword('');
      void upload(picked);
    },
    [upload],
  );

  const onDropRejected = useCallback(
    (rejections: FileRejection[]) => {
      const firstCode = rejections[0]?.errors[0]?.code;
      setIssue(firstCode === 'file-invalid-type' ? t('agentMd.pdfOnly') : t('agentMd.uploadFailed'));
    },
    [t],
  );

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
    onDropRejected,
    accept: { 'application/pdf': ['.pdf'] },
    maxFiles: 1,
    multiple: false,
    disabled: busy,
  });

  const submitPassword = () => {
    if (!file || !password || busy) return;
    void upload(file, password);
  };

  return (
    <section className="flex flex-col gap-4" data-testid="agent-md-step-upload">
      <p className="text-sm leading-6 text-muted-foreground">{t('agentMd.uploadHint')}</p>

      <div
        {...getRootProps()}
        aria-label={t('agentMd.dropHere')}
        aria-disabled={busy}
        className={cn(
          'group relative flex min-h-56 w-full cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed p-8 text-center transition-all duration-300 ease-out',
          isDragActive
            ? 'border-primary bg-primary/[0.04] ring-4 ring-primary/10'
            : 'border-border hover:border-foreground/15 hover:shadow-lg',
          busy && 'cursor-not-allowed opacity-65',
        )}
        data-testid="agent-md-dropzone"
      >
        <input {...getInputProps({ 'aria-label': t('agentMd.dropHere'), disabled: busy })} />
        <div className="mx-auto flex size-12 items-center justify-center rounded-[18px] bg-foreground text-background transition-transform duration-300 group-hover:scale-110">
          <svg className="size-6" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeWidth={1.5}
              d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
            />
          </svg>
        </div>
        <p className="text-base font-semibold tracking-[-0.02em]">{t('agentMd.dropHere')}</p>
        <p className="text-sm text-muted-foreground">{t('agentMd.pdfOnly')}</p>
      </div>

      {issue && (
        <div
          className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive"
          role="alert"
          data-testid="agent-md-upload-issue"
        >
          {issue}
        </div>
      )}

      {needPassword && (
        <form
          className="flex flex-col gap-2 rounded-2xl border border-border bg-card p-4"
          onSubmit={(e) => {
            e.preventDefault();
            submitPassword();
          }}
          data-testid="agent-md-password-form"
        >
          <label htmlFor="agent-md-password" className="text-sm font-medium text-foreground">
            {issue ?? t('agentMd.needPassword')}
          </label>
          <div className="flex gap-2">
            <Input
              id="agent-md-password"
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              placeholder="••••••"
              autoComplete="off"
              disabled={busy}
              data-testid="agent-md-password"
            />
            <Button type="submit" disabled={busy || !password} data-testid="agent-md-password-submit">
              {busy ? '…' : t('agentMd.passwordSubmit')}
            </Button>
          </div>
        </form>
      )}
    </section>
  );
}
