// Copyright 2026 LexMask Contributors

import { useEffect, useState } from 'react';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import type { AgentMdStatus } from '../api';

const POLL_INTERVAL_MS = 2000;
/** MinerU 解析实测基线（秒/页）：用于补偿性「预计还需」估算。 */
const PARSE_SECONDS_PER_PAGE = 4.6;

function formatElapsed(t: (key: string) => string, seconds: number): string {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  return `${minutes}${t('agentMd.minutes')} ${rest}${t('agentMd.seconds')}`;
}

interface StepProcessingProps {
  status: AgentMdStatus | null;
  /** 页面注入的单次轮询（内部已把就绪→评审等步骤切换好）。 */
  pollOnce: () => Promise<AgentMdStatus | null>;
  onError: (message: string) => void;
  /** state=failed 或轮询挂掉时回到上传步。 */
  onFail: () => void;
}

/**
 * 四步动线第二步：每 2s 轮询 /status。
 * sidecar 无页级进度（parsing 期间 pages_done 恒 0，服务端约束），故解析段做补偿性进度：
 * 已用时 + 总页数 + 按 4.6s/页 基线估算的剩余时间，配不定态进度条避免「卡死」观感（用户验收反馈）。
 */
export function StepProcessing({ status, pollOnce, onError, onFail }: StepProcessingProps) {
  const t = useT();
  const [elapsed, setElapsed] = useState(0);
  const [pollIssue, setPollIssue] = useState<string | null>(null);

  // 已用时计时器
  useEffect(() => {
    const startedAt = Date.now();
    const timer = window.setInterval(() => {
      setElapsed(Math.floor((Date.now() - startedAt) / 1000));
    }, 1000);
    return () => window.clearInterval(timer);
  }, []);

  // 状态轮询：出错即停（避免错误刷屏）；state=failed 为终态，同样停表直到重传（评审 R1）
  const failed = pollIssue !== null || status?.state === 'failed';
  useEffect(() => {
    if (failed) return;
    const poll = window.setInterval(() => {
      void pollOnce().catch((err: unknown) => {
        const message =
          err instanceof Error && err.message ? err.message : t('agentMd.statusFailed');
        setPollIssue(message);
        onError(message);
      });
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(poll);
  }, [failed, pollOnce, onError, t]);

  const failMessage = pollIssue ?? status?.message ?? '';
  const nerRunning = status?.stage === 'ner' || status?.state === 'ner_running';
  const pagesTotal = status?.pages_total ?? 0;
  const remainingMinutes =
    pagesTotal > 0
      ? Math.max(0, Math.ceil((pagesTotal * PARSE_SECONDS_PER_PAGE) / 60) - Math.floor(elapsed / 60))
      : 0;

  return (
    <section
      className="flex flex-col items-center gap-4 rounded-2xl border border-border bg-card p-10"
      data-testid="agent-md-step-processing"
    >
      {failed ? (
        <div className="flex flex-col items-center gap-3 text-center">
          <p className="text-base font-semibold text-destructive" data-testid="agent-md-failed">
            {t('agentMd.failed')}
          </p>
          {failMessage && <p className="max-w-lg text-sm text-muted-foreground">{failMessage}</p>}
          <Button variant="outline" size="sm" onClick={onFail} data-testid="agent-md-reupload">
            {t('agentMd.reupload')}
          </Button>
        </div>
      ) : (
        <div className="flex w-full max-w-md flex-col items-center gap-3 text-center">
          <div className="h-7 w-7 animate-spin rounded-full border-2 border-border border-t-foreground" />
          {nerRunning ? (
            <p className="text-base font-semibold" data-testid="agent-md-ner">
              {`${t('agentMd.ner')} · ${t('agentMd.elapsed')} ${formatElapsed(t, elapsed)}`}
            </p>
          ) : (
            <>
              <p className="text-base font-semibold" data-testid="agent-md-parsing">
                {`${t('agentMd.parsing')} · ${t('agentMd.elapsed')} ${formatElapsed(t, elapsed)}`}
              </p>
              {pagesTotal > 0 ? (
                <p className="text-sm text-muted-foreground" data-testid="agent-md-parse-estimate">
                  {`${t('agentMd.pagesTotalPrefix')} ${pagesTotal} ${t('agentMd.parseEstimate')} ${remainingMinutes} ${t('agentMd.minutes')}`}
                </p>
              ) : (
                <p className="text-sm text-muted-foreground" data-testid="agent-md-reading-doc">
                  {t('agentMd.readingDoc')}
                </p>
              )}
            </>
          )}
          {/* 不定态进度条：无页级进度时兜底，避免界面看起来冻结 */}
          <div className="h-2 w-full overflow-hidden rounded-full bg-muted" aria-hidden="true">
            <div className="h-full w-1/3 animate-pulse rounded-full bg-primary" />
          </div>
        </div>
      )}
    </section>
  );
}
