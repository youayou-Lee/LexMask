// Copyright 2026 LexMask Contributors

import { useEffect, useState } from 'react';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import type { AgentMdStatus } from '../api';

const POLL_INTERVAL_MS = 2000;

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
 * sidecar 无页级进度（parsing 期间 pages_done 恒 0），故解析段只展示已用时 +
 * 总页数（拿到即说明已开解析）；pages_total=0 时给排队提示（单卡串行等锁窗口）。
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
  const queued = (status?.pages_total ?? 0) === 0;

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
        <div className="flex flex-col items-center gap-3 text-center">
          <div className="h-7 w-7 animate-spin rounded-full border-2 border-border border-t-foreground" />
          {status?.state === 'ner_running' ? (
            <p className="text-base font-semibold" data-testid="agent-md-ner">
              {t('agentMd.ner')}
            </p>
          ) : (
            <>
              <p className="text-base font-semibold" data-testid="agent-md-parsing">
                {`${t('agentMd.parsing')} ${elapsed}s`}
              </p>
              {queued ? (
                <p className="text-sm text-muted-foreground">{t('agentMd.queueing')}</p>
              ) : (
                <p className="text-sm text-muted-foreground">
                  {`${t('agentMd.pagesTotal')} ${status?.pages_total ?? 0}`}
                </p>
              )}
            </>
          )}
        </div>
      )}
    </section>
  );
}
