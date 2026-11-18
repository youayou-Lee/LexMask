// Copyright 2026 LexMask Contributors

// Issue#262 A1：任务中心合并页——/jobs = 工单 + 结果双视图单入口，/history 重定向至此。
//
// 结构（计划已锁定的架构决策，勿改）：
// - TasksPage 在页面级实例化 useJobs() 一次（合并页本就是账本页，一次 GET /jobs）；
// - HistoryView（features/history）内部自实例化 useHistory() 且条件渲染——
//   惰性挂载：done 视图之外不发 /files 请求；
// - 外层 view（?view=active|review|done，缺省 active）只是预置 + 深链面：
//   active/review 映射 jobs 内层 tab（active / awaiting_review），仅随 view 变化
//   预置一次，用户仍可用既有筛选弹层改内层 tab；
// - active/review 页签计数取 pageMetrics.activeJobs / awaitingReviewItems，
//   done 页签不显示计数（history hook 惰性挂载，无计数来源）。
// JobsLedgerView = 原 features/jobs/index.tsx 的组装整体搬入（改接 props.jobs，
//   外层 saas-page/page-shell 上移至本页，jobs-page testid 保留在工单视图根上）。

import React from 'react';
import { useSearchParams } from 'react-router-dom';
import { RefreshCw, Trash2 } from 'lucide-react';
import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { InteractionLockOverlay } from '@/components/InteractionLockOverlay';
import { HistoryView } from '@/features/history';
import { useUiTheme } from '@/theme/ui-theme';
import { useJobs, type JobsStatusFilter } from './hooks/use-jobs';
import { useAgentMdTasks } from './hooks/use-agent-md-tasks';
import { AgentMdTaskPanel } from './components/agent-md-task-panel';
import { JobsFilters } from './components/jobs-filters';
import { JobsTable } from './components/jobs-table';
import { JobsPagination } from './components/jobs-pagination';

export type TasksView = 'active' | 'review' | 'done';

/** 外层 view → jobs 内层 tab 预置映射（done 走 History 视图，无内层 tab）。 */
const VIEW_TAB_MAP: Record<Exclude<TasksView, 'done'>, JobsStatusFilter> = {
  active: 'active',
  review: 'awaiting_review',
};

function readTasksView(raw: string | null): TasksView {
  return raw === 'review' || raw === 'done' ? raw : 'active';
}

type JobsState = ReturnType<typeof useJobs>;
type AgentMdTasksState = ReturnType<typeof useAgentMdTasks>;

/** 工单视图：features/jobs/index.tsx:29-74 的组装整体搬入，改接页面级 useJobs 结果。 */
function JobsLedgerView({
  jobs,
  agentTasks,
  view,
}: {
  jobs: JobsState;
  agentTasks: AgentMdTasksState;
  view: 'active' | 'review';
}) {
  const t = useT();
  const s = jobs;
  // Issue#355 冷评审 #3：ds 下筛选面板整体不渲染（信息上移标题区/指标条，
  // 刷新/清理按钮上移页签行右侧）；classic 分支保持原组装零漂移。
  const dsMode = useUiTheme((s2) => s2.theme) === 'ds';
  const lockLabel = s.deletingJobId
    ? t('jobs.deletingEllipsis')
    : s.requeueingJobId || s.cleanupLoading
      ? t('jobs.processingEllipsis')
      : t('job.status.processing');

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="jobs-page">
      {!dsMode && (
        <JobsFilters
          onRefresh={s.refreshList}
          onCleanup={() => s.setCleanupConfirmOpen(true)}
          refreshing={s.refreshing}
          tableBusy={s.tableBusy}
          metrics={s.pageMetrics}
        />
      )}

      {s.notice && (
        <Alert className="mb-2" data-testid="jobs-notice">
          <AlertDescription>{s.notice}</AlertDescription>
        </Alert>
      )}
      {s.err && (
        <Alert variant="destructive" className="mb-2" data-testid="jobs-error">
          <AlertDescription>{s.err}</AlertDescription>
        </Alert>
      )}

      {/* Issue#282：供料任务与工单并列，同样按状态进任务中心。 */}
      <AgentMdTaskPanel
        items={agentTasks.items}
        view={view}
        loading={agentTasks.loading}
        err={agentTasks.err}
        onRetry={agentTasks.refresh}
      />

      <JobsTable
        rows={s.rows}
        loading={s.loading}
        refreshing={s.refreshing}
        tableLoading={s.tableLoading}
        pageSize={s.rowsPageSize}
        deletingJobId={s.deletingJobId}
        tableBusy={s.tableBusy}
        onDelete={s.requestDelete}
        tab={s.tab}
        onTabChange={s.changeTab}
        stats={s.pageMetrics}
      />
      <div className="mt-2 shrink-0">
        <JobsPagination
          page={s.page}
          pageSize={s.pageSize}
          totalPages={s.totalPages}
          total={s.total}
          rangeStart={s.rangeStart}
          rangeEnd={s.rangeEnd}
          jumpPage={s.jumpPage}
          tableBusy={s.loading || s.tableLoading || s.interactionLocked}
          onGoPage={s.goPage}
          onChangePageSize={s.changePageSize}
          onJumpPageChange={s.setJumpPage}
        />
      </div>

      <ConfirmDialog
        open={s.cleanupConfirmOpen}
        title={t('jobs.cleanupTitle')}
        message={t('jobs.cleanupMessage')}
        danger
        onConfirm={s.onCleanup}
        onCancel={() => s.setCleanupConfirmOpen(false)}
      />
      {s.deleteCandidate && (
        <ConfirmDialog
          open
          title={t('jobs.deleteTask')}
          message={t('jobs.confirmDelete').replace(
            '{title}',
            s.deleteCandidate.title?.trim() || t('jobs.unnamedTask'),
          )}
          confirmText={t('jobs.deleteAction')}
          danger
          onConfirm={s.confirmDelete}
          onCancel={s.cancelDelete}
        />
      )}
      <InteractionLockOverlay active={s.interactionLocked} label={lockLabel} />
    </div>
  );
}

export function TasksPage() {
  const t = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const view = readTasksView(searchParams.get('view'));
  // Issue#355 验收期深化：ds 骨架（页面标题区/指标条）只在 ds 主题条件渲染——
  // classic 分支不产生任何新 DOM（比 CSS 显隐更强的零回归保证）。
  const dsMode = useUiTheme((s) => s.theme) === 'ds';

  // 工单 hook 在页面级实例化。Issue#268：
  // - initialTab 由 URL ?view= 注入（active→'active'、review→'awaiting_review'、
  //   done→'all'）——挂载即正确 tab，消除「首挂 'all' + 预置 changeTab 自纠」的双 GET；
  // - pollingEnabled：done 视图停 900ms 轮询（结果视图无需工单列表刷新），
  //   但保留首挂 GET 喂页签徽标（L196-208 消费 pageMetrics）。
  const jobs = useJobs({
    initialTab: view === 'done' ? 'all' : VIEW_TAB_MAP[view],
    pollingEnabled: view !== 'done',
  });
  const agentTasks = useAgentMdTasks();

  // 仅随 view 变化把外层视图预置进内层 tab 一次（done 不预置；用户仍可改内层 tab）。
  React.useEffect(() => {
    if (view === 'done') return;
    const targetTab = VIEW_TAB_MAP[view];
    if (targetTab !== jobs.tab) jobs.changeTab(targetTab);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 仅随 view 变化预置
  }, [view]);

  // tab→URL：缺省 view 移除参数（与 settings-hub / history 钩子的同步惯例一致）；
  // 其余 query（?source=&jobId= 深链）原样保留，往返页签不丢上下文。
  const selectView = (next: string) => {
    const nextParams = new URLSearchParams(searchParams);
    if (next === 'active') nextParams.delete('view');
    else nextParams.set('view', next);
    setSearchParams(nextParams, { replace: true });
  };

  const tabCountClass =
    'ml-1.5 rounded-full bg-muted px-1.5 py-0.5 text-[10px] font-semibold leading-none text-muted-foreground tabular-nums';

  return (
    <div
      className="jobs-root saas-page flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background"
      data-testid="tasks-page"
    >
      <div className="page-shell !max-w-[min(100%,2048px)] !px-3 !py-3 sm:!px-4 2xl:!px-5 flex min-h-0 min-w-0 flex-1 flex-col gap-2">
        {dsMode && (
          <div className="shrink-0" data-testid="ds-page-header">
            <h1 className="ds-page-title">{t('page.jobs.title')}</h1>
            <p className="ds-page-sub">{t('page.jobs.sub')}</p>
          </div>
        )}
        {dsMode && (
          <DsMetricsStrip
            activeJobs={jobs.pageMetrics.activeJobs + agentTasks.counts.active}
            awaitingReview={jobs.pageMetrics.awaitingReviewItems + agentTasks.counts.awaiting_review}
            completedJobs={jobs.pageMetrics.completedJobs}
            riskJobs={jobs.pageMetrics.riskJobs}
          />
        )}
        {/* Issue#355 冷评审 #3：ds 下页签与刷新/清理同一行（定稿工具行）；classic 维持上下两块 */}
        <div className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <Tabs value={view} onValueChange={selectView} className="shrink-0">
            <TabsList
              className="rounded-xl border border-border/70 bg-muted/40 p-1"
              data-testid="tasks-view-tabs"
            >
              <TabsTrigger
                value="active"
                className="whitespace-nowrap"
                data-testid="tasks-tab-active"
              >
                {t('tasks.tab.active')}
                <span className={tabCountClass}>
                  {jobs.pageMetrics.activeJobs + agentTasks.counts.active}
                </span>
              </TabsTrigger>
              <TabsTrigger
                value="review"
                className="whitespace-nowrap"
                data-testid="tasks-tab-review"
              >
                {t('tasks.tab.review')}
                <span className={tabCountClass}>
                  {jobs.pageMetrics.awaitingReviewItems + agentTasks.counts.awaiting_review}
                </span>
              </TabsTrigger>
              <TabsTrigger value="done" className="whitespace-nowrap" data-testid="tasks-tab-done">
                {t('tasks.tab.done')}
              </TabsTrigger>
            </TabsList>
          </Tabs>
          {dsMode && (
            <div className="flex shrink-0 items-center gap-2" data-testid="ds-tabs-actions">
              <Button
                variant="outline"
                size="sm"
                onClick={jobs.refreshList}
                disabled={jobs.tableBusy}
                data-testid="jobs-refresh-btn"
                title={t('jobs.refreshTitle')}
                className="h-8 min-w-[5.5rem] justify-center rounded-lg px-2.5 text-xs whitespace-nowrap"
              >
                <RefreshCw data-icon="inline-start" className={jobs.refreshing ? 'animate-spin' : ''} />
                {jobs.refreshing ? t('jobs.refreshing') : t('jobs.clickRefresh')}
              </Button>
              <Button
                variant="outline"
                size="sm"
                className="h-8 rounded-lg border-destructive/25 px-2.5 text-xs whitespace-nowrap text-destructive hover:bg-destructive/10"
                onClick={() => jobs.setCleanupConfirmOpen(true)}
                data-testid="jobs-cleanup-btn"
              >
                <Trash2 data-icon="inline-start" />
                {t('jobs.cleanupButton')}
              </Button>
            </div>
          )}
        </div>

        {/* 条件渲染（非 CSS 隐藏）：active/review 不发 /files，done 不重复抬列表请求。 */}
        {view === 'done' ? (
          <HistoryView />
        ) : (
          <JobsLedgerView
            jobs={jobs}
            agentTasks={agentTasks}
            view={view === 'review' ? 'review' : 'active'}
          />
        )}
      </div>
    </div>
  );
}

/** ds 指标条（定稿 §6.2）：单条通栏白卡 4 等分竖线分隔；口径=工单，与页签徽标同源。 */
function DsMetricsStrip({
  activeJobs,
  awaitingReview,
  completedJobs,
  riskJobs,
}: {
  activeJobs: number;
  awaitingReview: number;
  completedJobs: number;
  riskJobs: number;
}) {
  const t = useT();
  const cells = [
    {
      key: 'active',
      label: t('tasks.tab.active'),
      value: activeJobs,
      tone: 'neutral',
      note: t('ds.metrics.activeNote'),
    },
    {
      key: 'review',
      label: t('tasks.tab.review'),
      value: awaitingReview,
      tone: 'neutral',
      note: t('ds.metrics.reviewNote'),
    },
    {
      key: 'done',
      label: t('jobs.filter.completed'),
      value: completedJobs,
      tone: 'neutral',
      note: t('ds.metrics.doneNote'),
    },
    {
      key: 'risk',
      label: t('jobs.abnormalHeader'),
      value: riskJobs,
      tone: riskJobs > 0 ? 'danger' : 'neutral',
      note: t('ds.metrics.riskNote'),
    },
  ] as const;

  return (
    <div className="ds-metrics shrink-0" data-testid="ds-metrics-strip">
      {cells.map((cell) => (
        <div className="ds-metric" key={cell.key} data-testid={`ds-metric-${cell.key}`}>
          <span className="ds-metric-label">{cell.label}</span>
          <span className="ds-metric-value" data-tone={cell.tone}>
            {cell.value}
          </span>
          <span className="ds-metric-note">{cell.note}</span>
        </div>
      ))}
    </div>
  );
}
