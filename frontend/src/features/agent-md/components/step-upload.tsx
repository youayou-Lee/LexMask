// Copyright 2026 LexMask Contributors

import { useCallback, useState } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { useT } from '@/i18n';
import { UploadError, uploadWithProgress } from '../api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { cn } from '@/lib/utils';

interface StepUploadProps {
  onUploaded: (taskId: string) => void;
}

/**
 * 四步动线第一步：PDF 投递 + 加密 PDF 条件密码分支（Issue#75）。
 * 上传走 XHR（uploadWithProgress）拿真实上传进度——196MB 大文件 fetch 全程黑盒（用户验收反馈）。
 * 上传失败/拒收只走本步内的 issue 框展示（不喂页面横幅，评审 R1 去重——横幅留给后续步骤的错误）。
 */
export function StepUpload({ onUploaded }: StepUploadProps) {
  const t = useT();
  // file 留在组件态以便带密码重传；密码只存这里，不打日志、不进任何全局状态
  const [file, setFile] = useState<File | null>(null);
  const [needPassword, setNeedPassword] = useState(false);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  const [progress, setProgress] = useState<{ percent: number; loadedBytes: number; totalBytes: number } | null>(
    null,
  );

  const formatMb = (bytes: number) => `${Math.round(bytes / (1024 * 1024))}MB`;

  const upload = useCallback(
    async (target: File, pwd?: string) => {
      setBusy(true);
      setIssue(null);
      setProgress({ percent: 0, loadedBytes: 0, totalBytes: target.size });
      try {
        const data = await uploadWithProgress(target, pwd, (p) => setProgress(p));
        setPassword('');
        setNeedPassword(false);
        onUploaded(data.task_id);
      } catch (err) {
        if (err instanceof UploadError) {
          if (err.code === 'PDF_ENCRYPTED_NEEDS_PASSWORD') {
            setNeedPassword(true);
            setIssue(t('agentMd.needPassword'));
            return;
          }
          if (err.code === 'PDF_WRONG_PASSWORD') {
            setNeedPassword(true);
            setIssue(t('agentMd.wrongPassword'));
            return;
          }
          setIssue(`${t('agentMd.uploadFailed')}：${err.message}`);
          return;
        }
        // XHR 网络层失败等：同样给一句话反馈，绝不留死黑盒
        const message = err instanceof Error && err.message ? err.message : t('agentMd.uploadFailed');
        setIssue(`${t('agentMd.uploadFailed')}：${message}`);
      } finally {
        setBusy(false);
        setProgress(null);
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
      if (firstCode === 'file-invalid-type') {
        setIssue(t('agentMd.rejectNotPdf'));
        return;
      }
      if (firstCode === 'too-many-files') {
        setIssue(t('agentMd.rejectMultiple'));
        return;
      }
      setIssue(t('agentMd.rejectNotPdf'));
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

      {busy && progress && (
        <div
          className="flex flex-col gap-2 rounded-2xl border border-border bg-card p-4"
          data-testid="agent-md-upload-progress"
        >
          <p className="text-sm font-medium" data-testid="agent-md-upload-progress-text">
            {`${t('agentMd.uploadProgress')} ${progress.percent}% · ${formatMb(progress.loadedBytes)}/${formatMb(progress.totalBytes)}`}
          </p>
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
            <div
              className="h-full rounded-full bg-primary transition-all duration-200 ease-out"
              style={{ width: `${progress.percent}%` }}
              role="progressbar"
              aria-valuenow={progress.percent}
              aria-valuemin={0}
              aria-valuemax={100}
            />
          </div>
        </div>
      )}

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
