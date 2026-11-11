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
import { useT } from '@/i18n';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { InteractionLockOverlay } from '@/components/InteractionLockOverlay';
import { HistoryView } from '@/features/history';
import { useJobs, type JobsStatusFilter } from './hooks/use-jobs';
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

/** 工单视图：features/jobs/index.tsx:29-74 的组装整体搬入，改接页面级 useJobs 结果。 */
function JobsLedgerView({ jobs }: { jobs: JobsState }) {
  const t = useT();
  const s = jobs;
  const lockLabel = s.deletingJobId
    ? t('jobs.deletingEllipsis')
    : s.requeueingJobId || s.cleanupLoading
      ? t('jobs.processingEllipsis')
      : t('job.status.processing');

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col" data-testid="jobs-page">
      <JobsFilters
        onRefresh={s.refreshList}
        onCleanup={() => s.setCleanupConfirmOpen(true)}
        refreshing={s.refreshing}
        tableBusy={s.tableBusy}
        metrics={s.pageMetrics}
      />

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

  // 工单 hook 在页面级实例化（done 视图也不停轮询属于合并页既有预期，见任务计划）。
  const jobs = useJobs();

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
              <span className={tabCountClass}>{jobs.pageMetrics.activeJobs}</span>
            </TabsTrigger>
            <TabsTrigger
              value="review"
              className="whitespace-nowrap"
              data-testid="tasks-tab-review"
            >
              {t('tasks.tab.review')}
              <span className={tabCountClass}>{jobs.pageMetrics.awaitingReviewItems}</span>
            </TabsTrigger>
            <TabsTrigger value="done" className="whitespace-nowrap" data-testid="tasks-tab-done">
              {t('tasks.tab.done')}
            </TabsTrigger>
          </TabsList>
        </Tabs>

        {/* 条件渲染（非 CSS 隐藏）：active/review 不发 /files，done 不重复抬列表请求。 */}
        {view === 'done' ? <HistoryView /> : <JobsLedgerView jobs={jobs} />}
      </div>
    </div>
  );
}
