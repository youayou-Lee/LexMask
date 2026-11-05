// Copyright 2026 LexMask Contributors

// Issue#244：水印工具页面——上传 PDF → 检测报告 → 摘除 → 下载净化件。
// 结构照抄 restore-page（「输入 → 报告 → 结果」分卡；判定逻辑下沉 lib/watermark-tool
// 纯函数）。原件不会被修改：摘除产物是独立净化副本（watermark-clean 下载）。

import { useCallback, useState, type FC } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import { showToast } from '@/components/Toast';
import { authFetch } from '@/services/api-client';
import { localizeErrorMessage, messageFromEnvelope } from '@/utils/localizeError';
import { triggerDownload } from '@/features/playground/utils';
import { cn } from '@/lib/utils';
import {
  filenameFromContentDisposition,
  stripSummaryFields,
  watermarkCodeTextKey,
} from '@/lib/watermark-tool';
import {
  getWatermarkStatus,
  stripWatermark,
  watermarkCleanDownloadUrl,
  type WatermarkStatusReport,
  type WatermarkStripResult,
} from './watermarkToolApi';

const PDF_FILE_ACCEPT = { 'application/pdf': ['.pdf'] };

interface LoadedFile {
  fileId: string;
  filename: string;
  fileSize: number;
}

function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

export const WatermarkToolPage: FC = () => {
  const t = useT();

  const [file, setFile] = useState<LoadedFile | null>(null);
  const [uploading, setUploading] = useState(false);
  const [status, setStatus] = useState<WatermarkStatusReport | null>(null);
  const [probing, setProbing] = useState(false);
  const [statusError, setStatusError] = useState<string | null>(null);
  const [stripping, setStripping] = useState(false);
  const [stripResult, setStripResult] = useState<WatermarkStripResult | null>(null);
  const [stripError, setStripError] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);

  const probeStatus = useCallback(async (fileId: string) => {
    setProbing(true);
    setStatusError(null);
    try {
      const report = await getWatermarkStatus(fileId);
      setStatus(report);
    } catch (error) {
      // 检测失败同样不吞：报告区显式错误（信封文案），strip 按钮不出现
      setStatus(null);
      setStatusError(localizeErrorMessage(error, 'watermarkTool.probeFailed'));
    } finally {
      setProbing(false);
    }
  }, []);

  const onDrop = useCallback(
    async (accepted: File[], rejections: FileRejection[]) => {
      if (rejections.length > 0) {
        showToast(t('watermarkTool.fileReject'), 'error');
        return;
      }
      const picked = accepted[0];
      if (!picked || uploading) return;
      setUploading(true);
      setStatus(null);
      setStatusError(null);
      setStripResult(null);
      setStripError(null);
      try {
        const formData = new FormData();
        formData.append('file', picked);
        formData.append('upload_source', 'watermark-tool');
        const uploadRes = await authFetch('/api/v1/files/upload', {
          method: 'POST',
          body: formData,
        });
        if (!uploadRes.ok) {
          // 与 playground 上传同一信封读法：messageFromEnvelope 取后端文案，
          // 不把原始 JSON 甩给用户。
          let envelope: Parameters<typeof messageFromEnvelope>[0] = null;
          try {
            envelope = await uploadRes.json();
          } catch {
            /* 非 JSON 响应体：走本地化兜底文案 */
          }
          throw new Error(messageFromEnvelope(envelope, 'watermarkTool.uploadFailed'));
        }
        const uploadData = (await uploadRes.json()) as {
          file_id: string;
          filename: string;
          file_size: number;
        };
        setFile({
          fileId: uploadData.file_id,
          filename: uploadData.filename,
          fileSize: uploadData.file_size,
        });
        await probeStatus(uploadData.file_id);
      } catch (error) {
        setFile(null);
        setStatus(null);
        showToast(localizeErrorMessage(error, 'watermarkTool.uploadFailed'), 'error');
      } finally {
        setUploading(false);
      }
    },
    [probeStatus, t, uploading],
  );

  const dropzone = useDropzone({
    onDrop: (accepted, rejections) => void onDrop(accepted, rejections),
    accept: PDF_FILE_ACCEPT,
    maxFiles: 1,
    disabled: uploading,
  });

  const canStrip = status?.code === 'hit' && !stripping && !probing;

  const handleStrip = useCallback(async () => {
    if (!file) return;
    setStripping(true);
    setStripError(null);
    try {
      const result = await stripWatermark(file.fileId);
      setStripResult(result);
      if (result.code !== 'hit') {
        showToast(t('watermarkTool.stripNotHit'), 'info');
      }
    } catch (error) {
      setStripResult(null);
      setStripError(localizeErrorMessage(error, 'watermarkTool.stripFailed'));
    } finally {
      setStripping(false);
    }
  }, [file, t]);

  const summary = stripResult ? stripSummaryFields(stripResult) : null;
  const canDownload = Boolean(stripResult?.clean_available) && !downloading;

  const handleDownload = useCallback(async () => {
    if (!file || downloading) return;
    setDownloading(true);
    try {
      const res = await authFetch(watermarkCleanDownloadUrl(file.fileId));
      if (!res.ok) {
        throw new Error(`${res.status}`);
      }
      const blob = await res.blob();
      // 文件名优先后端 Content-Disposition（如「卷一-无水印.pdf」），
      // 取不到时回落本地化兜底名。
      const headerName = filenameFromContentDisposition(res.headers.get('content-disposition'));
      triggerDownload(blob, headerName || t('watermarkTool.cleanFilenameFallback'));
    } catch (error) {
      showToast(localizeErrorMessage(error, 'common.downloadFailed'), 'error');
    } finally {
      setDownloading(false);
    }
  }, [downloading, file, t]);

  const statusText = (code: unknown): string => {
    if (typeof code === 'string' && code === 'copy_dir_unwritable') {
      // strip 报告里可能出现的额外 fail-open 码：不在十个机读 code 里，走 unknown 键
      return t('watermarkTool.code.unknown');
    }
    return t(watermarkCodeTextKey(code));
  };

  return (
    <div
      className="saas-page flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background"
      data-testid="watermark-tool"
    >
      <div className="page-shell !max-w-[min(100%,1200px)] overflow-auto !px-3 !py-3 sm:!px-5 sm:!py-4 2xl:!px-8">
        <div className="page-stack gap-3">
          <section className="flex flex-none flex-wrap items-end justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <span className="saas-kicker">{t('watermarkTool.kicker')}</span>
              <h1 className="text-2xl font-semibold tracking-tight text-foreground">
                {t('watermarkTool.title')}
              </h1>
              <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
                {t('watermarkTool.subtitle')}
              </p>
            </div>
          </section>

          <section className="saas-panel flex flex-col gap-4 p-4 sm:p-5">
            <div className="space-y-2">
              <label className="text-sm font-semibold text-foreground">
                {t('watermarkTool.fileLabel')}
              </label>
              <div
                {...dropzone.getRootProps()}
                className={cn(
                  'flex cursor-pointer items-center justify-between gap-3 rounded-2xl border border-dashed border-border/70 bg-muted/20 px-4 py-3 transition-colors hover:bg-muted/40',
                  uploading && 'pointer-events-none opacity-70',
                )}
                data-testid="watermark-tool-upload"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-foreground">
                    {file
                      ? t('watermarkTool.fileLoaded').replace('{name}', file.filename)
                      : uploading
                        ? t('watermarkTool.uploading')
                        : t('watermarkTool.fileUpload')}
                  </p>
                  <p className="mt-0.5 truncate text-xs text-muted-foreground">
                    {t('watermarkTool.fileHint')}
                  </p>
                </div>
                <input {...dropzone.getInputProps()} />
              </div>
            </div>

            {file && (
              <div
                className="flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground"
                data-testid="watermark-tool-file-info"
              >
                <span className="font-medium text-foreground">{file.filename}</span>
                <span className="tabular-nums">{formatBytes(file.fileSize)}</span>
                <span>{t('watermarkTool.originalUntouched')}</span>
              </div>
            )}
          </section>

          {probing && (
            <p className="text-sm text-muted-foreground" data-testid="watermark-tool-probing">
              {t('watermarkTool.probing')}
            </p>
          )}

          {statusError && (
            <p className="text-sm text-[var(--error-foreground)]" data-testid="watermark-tool-status-error">
              {statusError}
            </p>
          )}

          {status && (
            <section className="saas-panel flex flex-col gap-4 p-4 sm:p-5" data-testid="watermark-tool-report">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-semibold text-foreground">
                  {t('watermarkTool.reportTitle')}
                </p>
              </div>
              <p
                className={cn(
                  'text-sm',
                  status.code === 'hit'
                    ? 'font-medium text-[var(--success-foreground)]'
                    : 'text-muted-foreground',
                )}
                data-testid="watermark-tool-code"
              >
                {statusText(status.code)}
              </p>
              {typeof status.error === 'string' && status.error && (
                <p className="break-all font-mono text-xs text-muted-foreground">{status.error}</p>
              )}
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <span data-testid="watermark-tool-pages-hit">
                  {t('watermarkTool.pagesHit').replace(
                    '{count}',
                    String(status.pages_hit ?? 0),
                  )}
                </span>
                <span>
                  {t('watermarkTool.pagesProbed').replace(
                    '{count}',
                    String(status.pages_probed ?? 0),
                  )}
                </span>
                {typeof status.elapsed_ms === 'number' && (
                  <span className="tabular-nums">
                    {t('watermarkTool.elapsed').replace('{ms}', String(status.elapsed_ms))}
                  </span>
                )}
              </div>
              {Array.isArray(status.fonts) && status.fonts.length > 0 && (
                <p className="break-all text-xs text-muted-foreground">
                  {t('watermarkTool.fonts').replace('{fonts}', status.fonts.join(', '))}
                </p>
              )}
              {canStrip && (
                <div>
                  <Button
                    type="button"
                    onClick={() => void handleStrip()}
                    disabled={stripping}
                    data-testid="watermark-tool-strip"
                    className="h-10 px-6"
                  >
                    {stripping ? t('watermarkTool.stripping') : t('watermarkTool.stripAction')}
                  </Button>
                </div>
              )}
            </section>
          )}

          {stripError && (
            <p className="text-sm text-[var(--error-foreground)]" data-testid="watermark-tool-strip-error">
              {stripError}
            </p>
          )}

          {stripResult && summary && (
            <section className="saas-panel flex flex-col gap-4 p-4 sm:p-5" data-testid="watermark-tool-result">
              <p className="text-sm font-semibold text-foreground">{t('watermarkTool.resultTitle')}</p>
              <p className="text-sm text-muted-foreground" data-testid="watermark-tool-result-code">
                {statusText(stripResult.code)}
              </p>
              <div className="flex flex-wrap gap-x-6 gap-y-1 text-xs text-muted-foreground">
                <span data-testid="watermark-tool-result-stripped">
                  {t('watermarkTool.resultStripped').replace(
                    '{count}',
                    String(summary.pagesStripped ?? 0),
                  )}
                </span>
                {summary.pagesHit !== null && (
                  <span>
                    {t('watermarkTool.pagesHit').replace('{count}', String(summary.pagesHit))}
                  </span>
                )}
                {summary.pagesSkipped !== null && summary.pagesSkipped > 0 && (
                  <span>
                    {t('watermarkTool.resultSkipped').replace(
                      '{count}',
                      String(summary.pagesSkipped),
                    )}
                  </span>
                )}
                {summary.guardRefused !== null && summary.guardRefused > 0 && (
                  <span>
                    {t('watermarkTool.resultGuardRefused').replace(
                      '{count}',
                      String(summary.guardRefused),
                    )}
                  </span>
                )}
                {summary.durationMs !== null && (
                  <span className="tabular-nums">
                    {t('watermarkTool.elapsed').replace('{ms}', String(summary.durationMs))}
                  </span>
                )}
              </div>
              {typeof stripResult.error === 'string' && stripResult.error && (
                <p className="break-all font-mono text-xs text-muted-foreground">
                  {stripResult.error}
                </p>
              )}
              {canDownload && (
                <div>
                  <Button
                    type="button"
                    variant="outline"
                    onClick={() => void handleDownload()}
                    disabled={downloading}
                    data-testid="watermark-tool-download"
                    className="h-10 px-6"
                  >
                    {downloading ? t('common.downloading') : t('watermarkTool.downloadClean')}
                  </Button>
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
};
