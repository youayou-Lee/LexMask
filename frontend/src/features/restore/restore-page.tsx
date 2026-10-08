// Copyright 2026 LexMask Contributors

// Issue#74：还原工具页面——脱敏文本 + 映射表 → 还原结果 UI（#50 T7 前端补全）。
// 「输入 → 结果」两段式；组件逻辑全部下沉 lib/ 纯函数（vitest node 环境无 jsdom）。

import { useCallback, useMemo, useState, type FC } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { showToast } from '@/components/Toast';
import { localizeErrorMessage } from '@/utils/localizeError';
import { triggerDownload } from '@/features/playground/utils';
import { cn } from '@/lib/utils';
import { restoreText } from './restoreApi';
import {
  exceedsSizeLimit,
  parseMappingJson,
  type MappingPrecheckResult,
} from './lib/mapping-guard';
import {
  buildAmbiguousRows,
  formatUnknownList,
  type RestoreResponse,
} from './lib/report-shape';
import { buildRestoreRequest, type RestorePolicy } from './lib/request';

const TEXT_FILE_ACCEPT = { 'text/plain': ['.txt', '.md'] };
const MAPPING_FILE_ACCEPT = { 'application/json': ['.json'] };

interface LoadedMapping {
  data: Record<string, unknown>;
  filename: string;
}

export const RestorePage: FC = () => {
  const t = useT();

  const [text, setText] = useState('');
  const [mapping, setMapping] = useState<LoadedMapping | null>(null);
  const [mappingError, setMappingError] = useState<string | null>(null);
  const [policy, setPolicy] = useState<RestorePolicy>('safe');
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [result, setResult] = useState<RestoreResponse | null>(null);
  // 次数列的口径是「该 key 在提交时的输入文本中出现几次」——提交后用户再改
  // 输入框也不影响已出结果的解读，故单独存快照。
  const [submittedText, setSubmittedText] = useState('');
  const [copied, setCopied] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [parseWarningsOpen, setParseWarningsOpen] = useState(true);

  const applyMappingPrecheck = useCallback(
    (precheck: MappingPrecheckResult, filename: string) => {
      if (precheck.ok) {
        setMapping({ data: precheck.mapping, filename });
        setMappingError(null);
      } else {
        setMapping(null);
        setMappingError(
          precheck.reason === 'invalidJson'
            ? t('restore.mappingInvalidJson').replace(
                '{detail}',
                precheck.detail ?? '',
              )
            : precheck.reason === 'notObject'
              ? t('restore.mappingNotObject')
              : t('restore.mappingEmpty'),
        );
      }
    },
    [t],
  );

  const onTextDrop = useCallback(
    async (accepted: File[], rejections: FileRejection[]) => {
      if (rejections.length > 0) {
        showToast(t('restore.textReject'), 'error');
        return;
      }
      const file = accepted[0];
      if (!file) return;
      const content = await file.text();
      if (exceedsSizeLimit(content)) {
        setSubmitError(t('restore.textTooLarge'));
        return;
      }
      setSubmitError(null);
      setText(content);
    },
    [t],
  );

  const textDropzone = useDropzone({
    onDrop: (accepted, rejections) => void onTextDrop(accepted, rejections),
    accept: TEXT_FILE_ACCEPT,
    maxFiles: 1,
    disabled: loading,
  });

  const onMappingDrop = useCallback(
    async (accepted: File[], rejections: FileRejection[]) => {
      if (rejections.length > 0) {
        showToast(t('restore.mappingReject'), 'error');
        return;
      }
      const file = accepted[0];
      if (!file) return;
      applyMappingPrecheck(parseMappingJson(await file.text()), file.name);
    },
    [applyMappingPrecheck, t],
  );

  const mappingDropzone = useDropzone({
    onDrop: (accepted, rejections) => void onMappingDrop(accepted, rejections),
    accept: MAPPING_FILE_ACCEPT,
    maxFiles: 1,
    disabled: loading,
  });

  const doRestore = useCallback(
    async (policyOverride?: RestorePolicy) => {
      const activePolicy = policyOverride ?? policy;
      // 防呆：点击校验，不满足就提示且不发请求（验收方案 v2 第 8 条）
      if (!text.trim()) {
        setSubmitError(t('restore.requiredText'));
        return;
      }
      if (!mapping) {
        setSubmitError(t('restore.requiredMapping'));
        return;
      }
      if (exceedsSizeLimit(text)) {
        setSubmitError(t('restore.textTooLarge'));
        return;
      }
      setSubmitError(null);
      setLoading(true);
      try {
        const response = await restoreText(
          buildRestoreRequest(text, mapping.data, activePolicy),
        );
        setResult(response);
        setSubmittedText(text);
        setCopied(false);
        setParseWarningsOpen(response.parse_warnings.length > 0);
      } catch (error) {
        setSubmitError(localizeErrorMessage(error, 'restore.apiError'));
      } finally {
        setLoading(false);
      }
    },
    [mapping, policy, t, text],
  );

  // 切档即重调：已有结果时切换 policy 立即用新档重跑（Issue 范围 2）
  const handlePolicyChange = useCallback(
    (next: RestorePolicy) => {
      if (next === policy) return;
      setPolicy(next);
      if (result) void doRestore(next);
    },
    [doRestore, policy, result],
  );

  const ambiguousRows = useMemo(
    () => (result ? buildAmbiguousRows(submittedText, result.ambiguous) : []),
    [result, submittedText],
  );
  const unknownList = useMemo(
    () => (result ? formatUnknownList(result.unknown) : null),
    [result],
  );

  const handleCopy = useCallback(() => {
    if (!result) return;
    void navigator.clipboard?.writeText(result.restored_text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 2000);
  }, [result]);

  const handleDownload = useCallback(() => {
    if (!result || downloading) return;
    setDownloading(true);
    try {
      triggerDownload(
        new Blob([result.restored_text], { type: 'text/markdown;charset=utf-8' }),
        'restored.md',
      );
    } finally {
      setDownloading(false);
    }
  }, [downloading, result]);

  return (
    <div
      className="saas-page flex h-full min-h-0 min-w-0 flex-col overflow-hidden bg-background"
      data-testid="restore"
    >
      <div className="page-shell !max-w-[min(100%,1200px)] overflow-auto !px-3 !py-3 sm:!px-5 sm:!py-4 2xl:!px-8">
        <div className="page-stack gap-3">
          <section className="flex flex-none flex-wrap items-end justify-between gap-3">
            <div className="min-w-0 space-y-1">
              <span className="saas-kicker">{t('restore.kicker')}</span>
              <h1 className="text-2xl font-semibold tracking-tight text-foreground">
                {t('restore.title')}
              </h1>
              <p className="max-w-3xl text-sm leading-6 text-muted-foreground">
                {t('restore.subtitle')}
              </p>
            </div>
          </section>

          <section className="saas-panel flex flex-col gap-4 p-4 sm:p-5">
            <div className="space-y-2">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <label className="text-sm font-semibold text-foreground">
                  {t('restore.textLabel')}
                </label>
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  className="h-8"
                  disabled={loading}
                  data-testid="restore-text-upload-btn"
                  {...textDropzone.getRootProps()}
                >
                  {t('restore.textUpload')}
                  <input {...textDropzone.getInputProps()} />
                </Button>
              </div>
              <Textarea
                value={text}
                onChange={(event) => setText(event.target.value)}
                placeholder={t('restore.textPlaceholder')}
                className="min-h-40 resize-y font-[system-ui] text-sm leading-relaxed"
                data-testid="restore-text-input"
              />
            </div>

            <div className="space-y-2">
              <label className="text-sm font-semibold text-foreground">
                {t('restore.mappingLabel')}
              </label>
              <div
                {...mappingDropzone.getRootProps()}
                className={cn(
                  'flex cursor-pointer items-center justify-between gap-3 rounded-2xl border border-dashed border-border/70 bg-muted/20 px-4 py-3 transition-colors hover:bg-muted/40',
                  mappingError && 'border-[var(--error-border)]',
                )}
                data-testid="restore-mapping-upload"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium text-foreground">
                    {mapping
                      ? t('restore.mappingLoaded').replace('{name}', mapping.filename)
                      : t('restore.mappingUpload')}
                  </p>
                  <p className="mt-0.5 truncate text-xs text-muted-foreground">
                    {mappingError ?? t('restore.mappingHint')}
                  </p>
                </div>
                {mapping && (
                  <svg
                    className="size-5 shrink-0 text-[var(--success-foreground)]"
                    fill="none"
                    stroke="currentColor"
                    viewBox="0 0 24 24"
                  >
                    <path
                      strokeLinecap="round"
                      strokeLinejoin="round"
                      strokeWidth={2}
                      d="M9 12l2 2 4-4m6 2a9 9 0 11-18 0 9 9 0 0118 0z"
                    />
                  </svg>
                )}
                <input {...mappingDropzone.getInputProps()} />
              </div>
            </div>

            <div className="space-y-2">
              <label className="text-sm font-semibold text-foreground">
                {t('restore.policyLabel')}
              </label>
              <div className="flex flex-wrap gap-2">
                <PolicyOption
                  selected={policy === 'safe'}
                  disabled={loading}
                  testId="restore-policy-safe"
                  title={t('restore.policySafe')}
                  desc={t('restore.policySafeDesc')}
                  onClick={() => handlePolicyChange('safe')}
                />
                <PolicyOption
                  selected={policy === 'first'}
                  disabled={loading}
                  testId="restore-policy-first"
                  title={t('restore.policyFirst')}
                  desc={t('restore.policyFirstDesc')}
                  onClick={() => handlePolicyChange('first')}
                />
              </div>
            </div>

            {submitError && (
              <p className="text-sm text-[var(--error-foreground)]" data-testid="restore-error">
                {submitError}
              </p>
            )}

            <div>
              <Button
                type="button"
                onClick={() => void doRestore()}
                disabled={loading}
                data-testid="restore-submit"
                className="h-10 px-6"
              >
                {loading ? t('restore.running') : t('restore.action')}
              </Button>
            </div>
          </section>

          {result && (
            <section className="saas-panel flex flex-col gap-4 p-4 sm:p-5" data-testid="restore-result">
              <div className="flex flex-wrap items-center justify-between gap-2">
                <p className="text-sm font-semibold text-foreground">
                  {t('restore.resultCount').replace('{count}', String(result.restored_count))}
                </p>
                <div className="flex items-center gap-2">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8"
                    onClick={handleCopy}
                    data-testid="restore-copy"
                  >
                    {copied ? t('restore.copied') : t('restore.copy')}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8"
                    onClick={handleDownload}
                    disabled={downloading}
                    data-testid="restore-download"
                  >
                    {downloading ? t('common.downloading') : t('restore.downloadMd')}
                  </Button>
                </div>
              </div>

              <div className="space-y-2">
                <label className="text-sm font-semibold text-foreground">
                  {t('restore.restoredTextLabel')}
                </label>
                <Textarea
                  readOnly
                  value={result.restored_text}
                  className="min-h-40 resize-y bg-muted/20 font-[system-ui] text-sm leading-relaxed"
                  data-testid="restore-result-text"
                />
              </div>

              {ambiguousRows.length > 0 && (
                <div className="space-y-2" data-testid="restore-ambiguous">
                  <p className="text-sm font-semibold text-foreground">
                    {t('restore.ambiguousTitle').replace('{count}', String(ambiguousRows.length))}
                  </p>
                  <div className="overflow-hidden rounded-2xl border border-border/70">
                    <table className="w-full text-left text-sm">
                      <thead className="bg-muted/30 text-xs text-muted-foreground">
                        <tr>
                          <th className="px-3 py-2 font-semibold">{t('restore.colKey')}</th>
                          <th className="px-3 py-2 font-semibold">{t('restore.colCandidates')}</th>
                          <th className="px-3 py-2 text-right font-semibold">
                            {t('restore.colCount')}
                          </th>
                        </tr>
                      </thead>
                      <tbody>
                        {ambiguousRows.map((row) => (
                          <tr key={row.key} className="border-t border-border/60">
                            <td className="px-3 py-2 align-top font-medium" data-testid="restore-ambiguous-key">
                              {row.key}
                            </td>
                            <td className="px-3 py-2 align-top text-muted-foreground">
                              {row.candidates.join('、')}
                              {row.reason && (
                                <span className="ml-1 text-xs opacity-70">（{row.reason}）</span>
                              )}
                            </td>
                            <td className="px-3 py-2 text-right align-top tabular-nums">
                              {row.occurrences}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                </div>
              )}

              {unknownList && unknownList.shown.length > 0 && (
                <div
                  className="tone-surface-danger rounded-2xl border px-4 py-3"
                  data-testid="restore-unknown"
                >
                  <p className="text-sm font-semibold text-[var(--error-foreground)]">
                    {t('restore.unknownTitle').replace('{count}', String(result.unknown.length))}
                  </p>
                  <p className="mt-1 text-xs text-muted-foreground">{t('restore.unknownHint')}</p>
                  <p className="mt-2 break-all font-mono text-xs text-foreground">
                    {unknownList.shown.join('、')}
                  </p>
                  {unknownList.overflow > 0 && (
                    <p className="mt-1 text-xs text-muted-foreground">
                      {t('restore.unknownOverflow').replace(
                        '{count}',
                        String(unknownList.overflow),
                      )}
                    </p>
                  )}
                </div>
              )}

              {result.parse_warnings.length > 0 && (
                <div data-testid="restore-parse-warnings">
                  <Button
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-8 w-full justify-between px-2"
                    onClick={() => setParseWarningsOpen((open) => !open)}
                  >
                    <span className="text-sm font-semibold">
                      {t('restore.parseWarningsTitle').replace(
                        '{count}',
                        String(result.parse_warnings.length),
                      )}
                    </span>
                    <svg
                      className={cn('size-4 transition-transform', parseWarningsOpen && 'rotate-180')}
                      fill="none"
                      stroke="currentColor"
                      viewBox="0 0 24 24"
                    >
                      <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M19 9l-7 7-7-7" />
                    </svg>
                  </Button>
                  {parseWarningsOpen && (
                    <ul className="mt-1 space-y-1 rounded-2xl border border-border/70 bg-muted/20 px-4 py-3 text-xs leading-5 text-muted-foreground">
                      {result.parse_warnings.map((warning, index) => (
                        <li key={index} className="break-all">
                          {warning}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </section>
          )}
        </div>
      </div>
    </div>
  );
};

const PolicyOption: FC<{
  selected: boolean;
  disabled: boolean;
  testId: string;
  title: string;
  desc: string;
  onClick: () => void;
}> = ({ selected, disabled, testId, title, desc, onClick }) => (
  <button
    type="button"
    onClick={onClick}
    disabled={disabled}
    data-testid={testId}
    className={cn(
      'min-w-0 flex-1 basis-56 rounded-2xl border px-4 py-2.5 text-left transition-colors disabled:opacity-60',
      selected
        ? 'border-foreground bg-foreground text-background'
        : 'border-border/70 bg-background hover:bg-accent',
    )}
  >
    <span className="block truncate text-sm font-semibold">{title}</span>
    <span className={cn('mt-0.5 block truncate text-xs', selected ? 'text-background/70' : 'text-muted-foreground')}>
      {desc}
    </span>
  </button>
);
