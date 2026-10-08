// Copyright 2026 LexMask Contributors

import { useEffect, useState } from 'react';
import { useT } from '@/i18n';
import { useNavigate } from 'react-router-dom';
import { agentMdApi } from '../api';
import { authFetch } from '@/services/api-client';
import { Button } from '@/components/ui/button';
import { ACTIVE_TASK_KEY } from '../agent-md-page';

/** GET /agent-md/{id}/artifacts/retained 的字段条目（agent_md_replace.retained_json）。 */
interface RetainedField {
  text: string;
  type: string;
  page: number;
}

const COPIED_RESET_MS = 2000;

interface StepResultProps {
  taskId: string;
  /** 上传新文档：清 localStorage 活跃任务键并回上传步（产物读不到/想换文档都有出口，用户验收反馈）。 */
  onNewUpload: () => void;
}

/** 四步动线第四步：MD 预览 + 一键复制 + 三件套下载 + 保留字段清单。 */
export function StepResult({ taskId, onNewUpload }: StepResultProps) {
  const t = useT();
  const navigate = useNavigate();
  const [md, setMd] = useState<string | null>(null);
  const [retained, setRetained] = useState<RetainedField[] | null>(null);
  // retained 拉不到 ≠ 空清单：区分展示（评审 R1），失败不打断结果页主内容
  const [retainedError, setRetainedError] = useState(false);
  const [copied, setCopied] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    const load = async () => {
      try {
        const [mdRes, retainedRes] = await Promise.all([
          authFetch(agentMdApi.artifactUrl(taskId, 'md')),
          authFetch(agentMdApi.artifactUrl(taskId, 'retained')),
        ]);
        if (!alive) return;
        if (!mdRes.ok) throw new Error(t('agentMd.artifactFailed'));
        setMd(await mdRes.text());
        // retained 拉不到走失败提示（不与空清单混淆），仍不挡结果页主内容
        if (retainedRes.ok) {
          const data = (await retainedRes.json()) as { retained_fields?: RetainedField[] };
          setRetained(Array.isArray(data.retained_fields) ? data.retained_fields : []);
        } else {
          setRetainedError(true);
          setRetained([]);
        }
      } catch (err) {
        if (alive) {
          setIssue(err instanceof Error && err.message ? err.message : t('agentMd.artifactFailed'));
        }
      }
    };
    void load();
    return () => {
      alive = false;
    };
  }, [taskId, t]);

  const copyMd = async () => {
    if (md == null) return;
    try {
      await navigator.clipboard.writeText(md);
      setCopied(true);
      window.setTimeout(() => setCopied(false), COPIED_RESET_MS);
    } catch {
      setIssue(t('agentMd.copyFailed'));
    }
  };

  return (
    <section className="flex flex-col gap-4" data-testid="agent-md-step-result">
      <h2 className="text-lg font-semibold tracking-tight">{t('agentMd.resultTitle')}</h2>

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => void copyMd()} disabled={md == null} data-testid="agent-md-copy">
          {copied ? t('agentMd.copied') : t('agentMd.copyMd')}
        </Button>
        <Button
          variant="secondary"
          onClick={() => {
            localStorage.removeItem(ACTIVE_TASK_KEY);
            onNewUpload();
          }}
          data-testid="agent-md-new-upload"
        >
          {t('agentMd.newUpload')}
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a href={agentMdApi.artifactUrl(taskId, 'md')} download data-testid="agent-md-download-md">
            {t('agentMd.downloadMd')}
          </a>
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a
            href={agentMdApi.artifactUrl(taskId, 'mapping')}
            download
            data-testid="agent-md-download-mapping"
          >
            {t('agentMd.downloadMapping')}
          </a>
        </Button>
        <Button variant="outline" size="sm" asChild>
          <a
            href={agentMdApi.artifactUrl(taskId, 'retained')}
            download
            data-testid="agent-md-download-retained"
          >
            {t('agentMd.downloadRetained')}
          </a>
        </Button>
      </div>

      {issue && (
        <div
          className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive"
          role="alert"
          data-testid="agent-md-result-issue"
        >
          {issue}
        </div>
      )}

      {md != null ? (
        <pre
          className="max-h-[28rem] overflow-auto whitespace-pre-wrap break-words rounded-2xl border border-border bg-muted/40 p-4 text-[13px] leading-6"
          data-testid="agent-md-md-preview"
        >
          {md}
        </pre>
      ) : (
        !issue && (
          <div className="h-7 w-7 animate-spin rounded-full self-center border-2 border-border border-t-foreground" />
        )
      )}

      <div className="rounded-2xl border border-border bg-card p-4" data-testid="agent-md-retained">
        <p className="text-sm font-medium text-foreground">{t('agentMd.retainedNote')}</p>
        {retainedError ? (
          <p className="mt-2 text-sm text-destructive" data-testid="agent-md-retained-error">
            {t('agentMd.artifactFailed')}
          </p>
        ) : retained == null ? null : retained.length === 0 ? (
          <p className="mt-2 text-sm text-muted-foreground">{t('agentMd.retainedEmpty')}</p>
        ) : (
          <ul className="mt-2 grid gap-1.5">
            {retained.map((field, index) => (
              <li
                key={`${field.page}-${field.text}-${index}`}
                className="flex flex-wrap items-baseline gap-x-2 text-sm"
              >
                <span className="font-medium text-foreground">{field.text}</span>
                <span className="text-xs text-muted-foreground">{field.type}</span>
                <span className="text-xs text-muted-foreground">P{field.page}</span>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button variant="outline" onClick={() => navigate('/restore')} data-testid="agent-md-go-restore">
          {t('agentMd.goRestore')}
        </Button>
        <p className="text-sm text-muted-foreground">{t('agentMd.restoreHint')}</p>
      </div>
    </section>
  );
}
