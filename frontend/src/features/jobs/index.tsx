// Copyright 2026 LexMask Contributors

// Issue#262 A1：/jobs 由任务中心合并页承接（工单 + 结果双视图）——
// 原 Jobs 组装整体搬入 ./tasks-page 的 JobsLedgerView，工单/结果两视图共用页面级
// useJobs；/history 路由重定向至 /jobs?view=done（router.tsx HistoryRedirect）。

export { TasksPage } from './tasks-page';
export { JobDetailPage } from './job-detail-page';
