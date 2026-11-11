// Copyright 2026 LexMask Contributors

// Issue#282：任务中心内的紧凑供料任务面板。
// 只读索引（GET /agent-md/tasks）：进行中 / 待审阅 / 已中断，点击行进供料页恢复。
// 已完成任务不在此列——它们从「已完成」视图的「供料」来源页签回看。

import { Link } from 'react-router-dom';
import { ArrowRight, Loader2 } from 'lucide-react';
import { useT } from '@/i18n';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import type { AgentMdTaskSummary } from '@/features/agent-md/api';

function taskStateKey(state: string): string {
  if (state === 'mapping_ready') return 'tasks.agentMd.state.review';
  if (state === 'failed') return 'tasks.agentMd.state.failed';
  return 'tasks.agentMd.state.active';
}

type AgentMdTaskPanelProps = {
  items: AgentMdTaskSummary[];
  loading: boolean;
  err: string | null;
  onRetry: () => void;
};

export function AgentMdTaskPanel({ items, loading, err, onRetry }: AgentMdTaskPanelProps) {
  const t = useT();
  if (!loading && !err && items.length === 0) return null;

  return (
    <section
      className="mb-2 shrink-0 rounded-xl border border-border/70 bg-muted/20 px-3 py-2"
      data-testid="agent-md-task-panel"
    >
      <div className="mb-1.5 flex items-center gap-2">
        <h2 className="text-xs font-semibold text-foreground">{t('tasks.agentMd.title')}</h2>
        {loading && items.length === 0 ? (
          <Loader2 className="size-3.5 animate-spin text-muted-foreground" aria-hidden />
        ) : null}
      </div>

      {err ? (
        <Alert variant="destructive" className="mb-1 py-1.5" data-testid="agent-md-task-error">
          <AlertDescription className="flex items-center justify-between gap-2 text-xs">
            <span>{err}</span>
            <button type="button" className="underline" onClick={onRetry}>
              {t('common.retry')}
            </button>
          </AlertDescription>
        </Alert>
      ) : null}

      {items.length > 0 ? (
        <ul className="divide-y divide-border/60">
          {items.map((task) => (
            <li key={task.task_id}>
              <Link
                to={`/agent-md?taskId=${encodeURIComponent(task.task_id)}`}
                className="flex items-center gap-3 py-1.5 text-xs hover:text-foreground"
                data-testid={`agent-md-task-${task.task_id}`}
              >
                <span
                  className={cn(
                    'min-w-0 flex-1 truncate',
                    task.state === 'failed' ? 'text-destructive' : 'text-foreground',
                  )}
                  title={task.filename}
                >
                  {task.filename}
                </span>
                {task.state === 'parsing' || task.state === 'ner_running' ? (
                  <span className="shrink-0 text-caption text-muted-foreground tabular-nums">
                    {t('tasks.agentMd.pages')
                      .replace('{done}', String(task.pages_done))
                      .replace('{total}', String(task.pages_total))}
                  </span>
                ) : null}
                <Badge
                  variant={task.state === 'failed' ? 'destructive' : 'secondary'}
                  className="shrink-0 rounded-full px-2 py-0.5 text-2xs"
                >
                  {t(taskStateKey(task.state))}
                </Badge>
                <ArrowRight className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
              </Link>
            </li>
          ))}
        </ul>
      ) : (
        <p className="py-1 text-xs text-muted-foreground">{t('tasks.agentMd.empty')}</p>
      )}
    </section>
  );
}
