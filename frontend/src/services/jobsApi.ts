// Copyright 2026 LexMask Contributors

import { get, post, put, del } from './api-client';
import type {
  BatchExportReport,
  BatchExportReportFile,
  BatchExportReportFileDeliveryStatus,
  BatchExportReportSummary,
  BatchExportReportSummaryDeliveryStatus,
  BatchExportReportVisualEvidence,
  BatchExportReportVisualReview,
  JobExportReportJob,
  JobExportReportRedactedZip,
} from '../types';

export type JobTypeApi =
  | 'text_batch'
  | 'image_batch'
  | 'smart_batch'
  | 'structured_batch'
  | 'agent_batch'
  | 'single';
export type JobStatusFilterApi =
  | 'active'
  | 'awaiting_review'
  | 'completed'
  | 'risk'
  | 'draft';

export type JobProgress = {
  total_items: number;
  pending: number;
  processing: number;
  queued: number;
  parsing: number;
  ner: number;
  vision: number;
  awaiting_review: number;
  review_approved: number;
  redacting: number;
  completed: number;
  failed: number;
  cancelled?: number;
};

export type JobItemRow = {
  id: string;
  job_id: string;
  file_id: string;
  sort_order: number;
  status: string;
  filename?: string;
  file_type?: string;
  has_output?: boolean;
  entity_count?: number;
  has_review_draft?: boolean;
  review_draft_updated_at?: string | null;
  progress_stage?: string | null;
  progress_current?: number;
  progress_total?: number;
  progress_message?: string | null;
  progress_updated_at?: string | null;
  error_message?: string | null;
  reviewed_at?: string | null;
  reviewer?: string | null;
  /**
   * Issue#257：页级视图入口（轻量 `?performance=false` 详情也带，无需拉全量 performance）。
   * `paged` 仅 pdf / pdf_scanned / image 为 true；`recognition_failed_pages` 是 #199 逐页失败账本。
   */
  paged?: boolean;
  page_count?: number;
  recognition_success_pages?: number;
  recognition_failed_pages?: number[];
  /**
   * Issue#194：原始 performance_json（getJob 默认 performance=true 时透出）。
   * 处理详情卡消费；轻量模式（performance:false）下为 null。
   */
  performance?: Record<string, unknown> | null;
  created_at: string;
  updated_at: string;
};

export type JobItemReviewDraft = {
  exists?: boolean;
  entities: Array<Record<string, unknown>>;
  bounding_boxes: Array<Record<string, unknown>>;
  updated_at?: string | null;
  degraded?: boolean;
  retry_after_ms?: number | null;
};

export type JobSummary = {
  id: string;
  job_type: JobTypeApi;
  title: string;
  status: string;
  skip_item_review: boolean;
  config: Record<string, unknown>;
  error_message?: string | null;
  created_at: string;
  updated_at: string;
  progress: JobProgress;

  nav_hints?: {
    item_count: number;
    first_awaiting_review_item_id?: string | null;
    wizard_furthest_step?: number | null;
    batch_step1_configured?: boolean | null;
    redacted_count?: number | null;
    awaiting_review_count?: number | null;
  };
};

export type JobDetail = JobSummary & { items: JobItemRow[] };

export type JobListStats = {
  total_jobs: number;
  draft_jobs: number;
  active_jobs: number;
  awaiting_review_jobs: number;
  completed_jobs: number;
  risk_jobs: number;
  total_items: number;
  active_items: number;
  awaiting_review_items: number;
  completed_items: number;
  risk_items: number;
};

export type DeleteJobResult = {
  id: string;
  deleted: boolean;
  deleted_item_count: number;
  detached_file_count: number;
};

const REVIEW_DRAFT_READ_TIMEOUT_MS = 5_000;
const FALLBACK_DATE = new Date(0).toISOString();

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function asRecord(value: unknown): Record<string, unknown> {
  return isRecord(value) ? value : {};
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function asNullableString(value: unknown): string | null {
  return typeof value === 'string' || value === null ? value : null;
}

function asOptionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined;
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function firstNumber(values: unknown[], fallback = 0): number {
  for (const value of values) {
    const parsed = asNumber(value, Number.NaN);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function firstString(values: unknown[], fallback = ''): string {
  for (const value of values) {
    if (typeof value === 'string' && value.trim() !== '') return value;
  }
  return fallback;
}

function nullableNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function nullableBoolean(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function asStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === 'string' ? item : null))
    .filter((item): item is string => item !== null);
}

function asNumberRecord(value: unknown): Record<string, number> {
  const record = asRecord(value);
  return Object.fromEntries(
    Object.entries(record)
      .map(([key, raw]) => [key, asNumber(raw, Number.NaN)] as const)
      .filter((entry): entry is readonly [string, number] => Number.isFinite(entry[1])),
  );
}

function asNullableNumber(value: unknown): number | null {
  const parsed = asNumber(value, Number.NaN);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizeJobType(raw: unknown): JobTypeApi {
  if (
    raw === 'text_batch' ||
    raw === 'image_batch' ||
    raw === 'smart_batch' ||
    raw === 'structured_batch' ||
    raw === 'agent_batch' ||
    raw === 'single'
  )
    return raw;
  if (raw === 'text') return 'text_batch';
  if (raw === 'image') return 'image_batch';
  if (raw === 'smart') return 'smart_batch';
  if (raw === 'structured') return 'structured_batch';
  if (raw === 'agent') return 'agent_batch';
  return 'text_batch';
}

function normalizeJobProgress(job: Record<string, unknown>): JobProgress {
  const progress = asRecord(job.progress);

  return {
    total_items: firstNumber([progress.total_items, job.item_count, job.total_items]),
    pending: firstNumber([progress.pending, job.pending_count]),
    processing: firstNumber([progress.processing, job.processing_count]),
    queued: firstNumber([progress.queued, job.queued_count]),
    parsing: firstNumber([progress.parsing, job.parsing_count]),
    ner: firstNumber([progress.ner, job.ner_count]),
    vision: firstNumber([progress.vision, job.vision_count]),
    awaiting_review: firstNumber([progress.awaiting_review, job.awaiting_review_count]),
    review_approved: firstNumber([progress.review_approved, job.review_approved_count]),
    redacting: firstNumber([progress.redacting, job.redacting_count]),
    completed: firstNumber([progress.completed, job.completed_count]),
    failed: firstNumber([progress.failed, job.failed_count]),
    cancelled: firstNumber([progress.cancelled, job.cancelled_count], 0),
  };
}

function normalizeNavHints(
  job: Record<string, unknown>,
  progress: JobProgress,
): JobSummary['nav_hints'] {
  const navHints = asRecord(job.nav_hints);

  return {
    item_count: firstNumber([navHints.item_count, job.item_count, progress.total_items]),
    first_awaiting_review_item_id: asNullableString(navHints.first_awaiting_review_item_id),
    wizard_furthest_step: nullableNumber(navHints.wizard_furthest_step),
    batch_step1_configured: nullableBoolean(navHints.batch_step1_configured),
    redacted_count: nullableNumber(navHints.redacted_count),
    awaiting_review_count:
      nullableNumber(navHints.awaiting_review_count) ?? progress.awaiting_review,
  };
}

function normalizeJobItemRow(rawItem: unknown): JobItemRow {
  const item = asRecord(rawItem);
  const createdAt = asString(item.created_at, FALLBACK_DATE);

  return {
    id: asString(item.id),
    job_id: asString(item.job_id),
    file_id: asString(item.file_id),
    sort_order: asNumber(item.sort_order),
    status: asString(item.status, 'pending'),
    filename: asOptionalString(item.filename),
    file_type: asOptionalString(item.file_type),
    has_output: asBoolean(item.has_output),
    entity_count: asNumber(item.entity_count),
    has_review_draft: asBoolean(item.has_review_draft),
    review_draft_updated_at: asNullableString(item.review_draft_updated_at),
    progress_stage: asNullableString(item.progress_stage),
    progress_current: asNumber(item.progress_current),
    progress_total: asNumber(item.progress_total),
    progress_message: asNullableString(item.progress_message),
    progress_updated_at: asNullableString(item.progress_updated_at),
    error_message: asNullableString(item.error_message),
    reviewed_at: asNullableString(item.reviewed_at),
    reviewer: asNullableString(item.reviewer),
    paged: item.paged === true,
    page_count: asNumber(item.page_count),
    recognition_success_pages:
      typeof item.recognition_success_pages === 'number' ? item.recognition_success_pages : undefined,
    recognition_failed_pages: Array.isArray(item.recognition_failed_pages)
      ? item.recognition_failed_pages.filter((value): value is number => typeof value === 'number')
      : [],
    performance: isRecord(item.performance) ? item.performance : null,
    created_at: createdAt,
    updated_at: asString(item.updated_at, createdAt),
  };
}

export function normalizeJobSummary(rawJob: unknown): JobSummary {
  const job = asRecord(rawJob);
  const progress = normalizeJobProgress(job);
  const createdAt = asString(job.created_at, FALLBACK_DATE);

  return {
    id: firstString([job.id, job.job_id]),
    job_type: normalizeJobType(job.job_type ?? job.type),
    title: firstString([job.title, job.name], 'Untitled job'),
    status: asString(job.status, 'draft'),
    skip_item_review: asBoolean(job.skip_item_review),
    config: asRecord(job.config),
    error_message: asNullableString(job.error_message),
    created_at: createdAt,
    updated_at: asString(job.updated_at, createdAt),
    progress,
    nav_hints: normalizeNavHints(job, progress),
  };
}

export function normalizeJobDetail(rawJob: unknown): JobDetail {
  const job = asRecord(rawJob);
  const items = Array.isArray(job.items) ? job.items.map(normalizeJobItemRow) : [];

  return {
    ...normalizeJobSummary(job),
    items,
  };
}

function normalizeExportReportJob(rawJob: unknown): JobExportReportJob | null {
  const job = asRecord(rawJob);
  const id = asString(job.id);
  if (!id) return null;

  return {
    id,
    job_type: asString(job.job_type),
    status: asString(job.status),
    skip_item_review: asBoolean(job.skip_item_review),
    config: asRecord(job.config),
  };
}

function normalizeExportReportVisualReview(rawReview: unknown): BatchExportReportVisualReview {
  const review = asRecord(rawReview);
  const byIssue = asNumberRecord(review.by_issue);
  const issueCount = asNumber(review.issue_count);
  const issuePages = asStringArray(review.issue_pages);
  const issueLabels = asStringArray(review.issue_labels);

  return {
    blocking: asBoolean(review.blocking),
    review_hint: asBoolean(review.review_hint, issueCount > 0),
    issue_count: issueCount,
    issue_pages: issuePages,
    issue_pages_count: asNumber(review.issue_pages_count, issuePages.length),
    issue_labels: issueLabels,
    by_issue: byIssue,
  };
}

function normalizeExportReportVisualEvidence(
  rawEvidence: unknown,
): BatchExportReportVisualEvidence | undefined {
  if (!isRecord(rawEvidence)) return undefined;
  return {
    total_boxes: asNumber(rawEvidence.total_boxes),
    selected_boxes: asNumber(rawEvidence.selected_boxes),
    visual_feature_model: asNumber(rawEvidence.visual_feature_model),
    local_fallback: asNumber(rawEvidence.local_fallback),
    ocr_has: asNumber(rawEvidence.ocr_has),
    table_structure: asNumber(rawEvidence.table_structure),
    fallback_detector: asNumber(rawEvidence.fallback_detector),
    source_counts: asNumberRecord(rawEvidence.source_counts),
    evidence_source_counts: asNumberRecord(rawEvidence.evidence_source_counts),
    source_detail_counts: asNumberRecord(rawEvidence.source_detail_counts),
    warnings_by_key: asNumberRecord(rawEvidence.warnings_by_key),
  };
}

function normalizeExportReportSummaryDeliveryStatus(
  rawSummary: Record<string, unknown>,
  selectedFiles: number,
  actionRequiredFiles: number,
): BatchExportReportSummaryDeliveryStatus {
  const rawStatus = asString(rawSummary.delivery_status);
  if (
    rawStatus === 'ready_for_delivery' ||
    rawStatus === 'action_required' ||
    rawStatus === 'no_selection'
  ) {
    return rawStatus;
  }
  if (selectedFiles === 0) return 'no_selection';
  if (asBoolean(rawSummary.ready_for_delivery, actionRequiredFiles === 0)) {
    return 'ready_for_delivery';
  }
  return 'action_required';
}

function normalizeExportReportFileDeliveryStatus(
  rawFile: Record<string, unknown>,
  selectedForExport: boolean,
): BatchExportReportFileDeliveryStatus {
  const rawStatus = asString(rawFile.delivery_status);
  if (
    rawStatus === 'ready_for_delivery' ||
    rawStatus === 'action_required' ||
    rawStatus === 'not_selected'
  ) {
    return rawStatus;
  }
  if (!selectedForExport) return 'not_selected';
  return asBoolean(rawFile.ready_for_delivery) ? 'ready_for_delivery' : 'action_required';
}

function normalizeExportReportSummary(rawSummary: unknown): BatchExportReportSummary {
  const summary = asRecord(rawSummary);
  const selectedFiles = asNumber(summary.selected_files);
  const actionRequiredFiles = asNumber(summary.action_required_files);
  const blockingFiles = asNumber(summary.blocking_files, actionRequiredFiles);
  const visualReviewIssueCount = asNumber(summary.visual_review_issue_count);
  const deliveryStatus = normalizeExportReportSummaryDeliveryStatus(
    summary,
    selectedFiles,
    actionRequiredFiles,
  );
  const actionRequired = deliveryStatus === 'action_required';

  return {
    total_files: asNumber(summary.total_files),
    selected_files: selectedFiles,
    redacted_selected_files: asNumber(summary.redacted_selected_files),
    unredacted_selected_files: asNumber(summary.unredacted_selected_files),
    review_confirmed_selected_files: asNumber(summary.review_confirmed_selected_files),
    failed_selected_files: asNumber(summary.failed_selected_files),
    detected_entities: asNumber(summary.detected_entities),
    redaction_coverage: asNumber(summary.redaction_coverage),
    delivery_status: deliveryStatus,
    action_required_files: actionRequiredFiles,
    action_required: actionRequired,
    blocking_files: blockingFiles,
    blocking: actionRequired,
    ready_for_delivery: deliveryStatus === 'ready_for_delivery',
    by_status: asNumberRecord(summary.by_status),
    zip_redacted_included_files: asNumber(summary.zip_redacted_included_files),
    zip_redacted_skipped_files: asNumber(summary.zip_redacted_skipped_files),
    visual_review_hint: asBoolean(summary.visual_review_hint, visualReviewIssueCount > 0),
    visual_review_issue_files: asNumber(summary.visual_review_issue_files),
    visual_review_issue_count: visualReviewIssueCount,
    visual_review_issue_pages_count: asNumber(summary.visual_review_issue_pages_count),
    visual_review_issue_labels: asStringArray(summary.visual_review_issue_labels),
    visual_review_by_issue: asNumberRecord(summary.visual_review_by_issue),
    visual_evidence: normalizeExportReportVisualEvidence(summary.visual_evidence),
  };
}

function normalizeExportReportFile(rawFile: unknown): BatchExportReportFile {
  const file = asRecord(rawFile);
  const visualReview = normalizeExportReportVisualReview(file.visual_review);
  const selectedForExport = asBoolean(file.selected_for_export);
  const deliveryStatus = normalizeExportReportFileDeliveryStatus(file, selectedForExport);
  const readyForDelivery = deliveryStatus === 'ready_for_delivery';
  const actionRequired = deliveryStatus !== 'ready_for_delivery';

  return {
    item_id: asString(file.item_id),
    file_id: asString(file.file_id),
    filename: asString(file.filename),
    file_type: asString(file.file_type),
    file_size: asNumber(file.file_size),
    status: asString(file.status, 'unknown'),
    has_output: asBoolean(file.has_output),
    review_confirmed: asBoolean(file.review_confirmed),
    entity_count: asNumber(file.entity_count),
    page_count: asNullableNumber(file.page_count),
    selected_for_export: selectedForExport,
    delivery_status: deliveryStatus,
    error: asNullableString(file.error),
    ready_for_delivery: readyForDelivery,
    action_required: actionRequired,
    blocking: actionRequired,
    blocking_reasons: asStringArray(file.blocking_reasons),
    redacted_export_skip_reason: asNullableString(file.redacted_export_skip_reason),
    visual_review_hint: asBoolean(file.visual_review_hint, visualReview.review_hint),
    visual_evidence: normalizeExportReportVisualEvidence(file.visual_evidence),
    visual_review: visualReview,
  };
}

function normalizeExportReportRedactedZip(rawZip: unknown): JobExportReportRedactedZip {
  const zip = asRecord(rawZip);
  const skipped = Array.isArray(zip.skipped)
    ? zip.skipped.map((item) => {
        const skippedItem = asRecord(item);
        return {
          file_id: asString(skippedItem.file_id),
          reason: asString(skippedItem.reason),
        };
      })
    : [];

  return {
    included_count: asNumber(zip.included_count),
    skipped_count: asNumber(zip.skipped_count, skipped.length),
    skipped,
  };
}

export function normalizeJobExportReport(rawReport: unknown): BatchExportReport {
  const report = asRecord(rawReport);
  const files = Array.isArray(report.files) ? report.files.map(normalizeExportReportFile) : [];

  return {
    generated_at: asString(report.generated_at, FALLBACK_DATE),
    job: normalizeExportReportJob(report.job),
    summary: normalizeExportReportSummary(report.summary),
    redacted_zip: normalizeExportReportRedactedZip(report.redacted_zip),
    files,
  };
}

export function createJob(body: {
  job_type: JobTypeApi;
  title?: string;
  config?: Record<string, unknown>;
  skip_item_review?: boolean;
}): Promise<JobSummary> {
  return post('/jobs', body).then(normalizeJobSummary);
}

export function updateJobDraft(
  jobId: string,
  body: { title?: string; config?: Record<string, unknown>; skip_item_review?: boolean },
): Promise<JobSummary> {
  return put(`/jobs/${encodeURIComponent(jobId)}`, body).then(normalizeJobSummary);
}

export async function listJobs(params: {
  job_type?: JobTypeApi;
  status?: JobStatusFilterApi;
  page?: number;
  page_size?: number;
}): Promise<{
  jobs: JobSummary[];
  total: number;
  page: number;
  page_size: number;
  stats: JobListStats;
}> {
  const response = asRecord(await get('/jobs', { params }));
  const jobs = Array.isArray(response.jobs) ? response.jobs.map(normalizeJobSummary) : [];
  const stats = asRecord(response.stats);

  return {
    jobs,
    total: asNumber(response.total, jobs.length),
    page: asNumber(response.page, params.page ?? 1),
    page_size: asNumber(response.page_size, params.page_size ?? jobs.length),
    stats: {
      total_jobs: asNumber(stats.total_jobs, asNumber(response.total, jobs.length)),
      draft_jobs: asNumber(stats.draft_jobs),
      active_jobs: asNumber(stats.active_jobs),
      awaiting_review_jobs: asNumber(stats.awaiting_review_jobs),
      completed_jobs: asNumber(stats.completed_jobs),
      risk_jobs: asNumber(stats.risk_jobs),
      total_items: asNumber(stats.total_items),
      active_items: asNumber(stats.active_items),
      awaiting_review_items: asNumber(stats.awaiting_review_items),
      completed_items: asNumber(stats.completed_items),
      risk_items: asNumber(stats.risk_items),
    },
  };
}

export function getJob(
  jobId: string,
  options?: { performance?: boolean },
): Promise<JobDetail> {
  // performance:false = 轻量模式。万级任务的全量详情（含每文件性能数据）
  // 可达 38MB；向导恢复与轮询只需要 items 的状态字段。
  const suffix = options?.performance === false ? '?performance=false' : '';
  return get(`/jobs/${encodeURIComponent(jobId)}${suffix}`).then(normalizeJobDetail);
}

export function getJobExportReport(
  jobId: string,
  fileIds: string[],
  summaryOnly = false,
): Promise<BatchExportReport> {
  const query = new URLSearchParams();
  fileIds.forEach((fileId) => query.append('file_ids', fileId));
  if (summaryOnly) query.append('summary_only', 'true');
  const suffix = query.toString();
  return get(`/jobs/${encodeURIComponent(jobId)}/export-report${suffix ? `?${suffix}` : ''}`).then(
    normalizeJobExportReport,
  );
}

export async function getJobsBatch(ids: string[]): Promise<{ jobs: JobDetail[] }> {
  const response = asRecord(await post('/jobs/batch-details', { ids }));
  const jobs = Array.isArray(response.jobs) ? response.jobs.map(normalizeJobDetail) : [];

  return { jobs };
}

export function submitJob(jobId: string): Promise<JobSummary> {
  return post(`/jobs/${encodeURIComponent(jobId)}/submit`).then(normalizeJobSummary);
}

export function cancelJob(jobId: string): Promise<JobSummary> {
  return post(`/jobs/${encodeURIComponent(jobId)}/cancel`).then(normalizeJobSummary);
}

export function requeueFailed(jobId: string): Promise<JobSummary> {
  return post(`/jobs/${encodeURIComponent(jobId)}/requeue-failed`).then(normalizeJobSummary);
}

export function deleteJob(jobId: string): Promise<DeleteJobResult> {
  return del<DeleteJobResult>(`/jobs/${encodeURIComponent(jobId)}`);
}

export function deleteJobItem(
  jobId: string,
  itemId: string,
): Promise<{ deleted: boolean; item_id: string; file_id: string | null }> {
  return del(`/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}`);
}

export function getItemReviewDraft(jobId: string, itemId: string): Promise<JobItemReviewDraft> {
  return get<JobItemReviewDraft>(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/review-draft`,
    { timeout: REVIEW_DRAFT_READ_TIMEOUT_MS },
  );
}

/** 供料批量（Issue#196）：映射表决策（只送改动项，与单文件 buildDecisions 同构）。 */
export type AgentMdDecision = { id: string; action: 'keep' | 'exclude' | 'custom'; replacement?: string };

export type AgentMdMappingItem = {
  id: string;
  original_text: string;
  entity_type: string;
  replacement: string;
  excluded: boolean;
};

/** 供料批量：读 item 的映射表（后端已就地套用已保存的草稿决策）。 */
export function getAgentMdMapping(jobId: string, itemId: string): Promise<{ items: AgentMdMappingItem[] }> {
  return get<{ items: AgentMdMappingItem[] }>(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/agent-md-mapping`,
  );
}

export function putItemReviewDraft(
  jobId: string,
  itemId: string,
  body: {
    entities: Array<Record<string, unknown>>;
    bounding_boxes: Array<Record<string, unknown>>;
    agent_md_decisions?: AgentMdDecision[];
  },
): Promise<JobItemReviewDraft> {
  return put<JobItemReviewDraft>(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/review-draft`,
    body,
  );
}

export function commitItemReview(
  jobId: string,
  itemId: string,
  body: {
    entities: Array<Record<string, unknown>>;
    bounding_boxes: Array<Record<string, unknown>>;
    agent_md_decisions?: AgentMdDecision[];
  },
): Promise<JobItemRow> {
  return post<JobItemRow>(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/review/commit`,
    body,
  );
}

export type CommitAllReviewsResult = {
  job_id: string;
  total_awaiting: number;
  confirmed: number;
  failed: Array<{ item_id: string; error: string }>;
  /** 万级 job 后台执行：true = 已受理，进度靠轮询 已确认 计数 */
  started?: boolean;
};

/** Batch one-click confirm: approve every awaiting-review item in a job at once. */
export function commitAllReviews(jobId: string): Promise<CommitAllReviewsResult> {
  return post<CommitAllReviewsResult>(
    `/jobs/${encodeURIComponent(jobId)}/review/commit-all`,
    {},
  );
}

// ─── Issue#257 一期：页级韧性（页状态 / 部分导出 / 导出记录）───────────────
//
// 「页格三态」的数据源：后端 page-status 端点把 performance.recognition.pages
// 投影成每页一条 {page,state,reason}，失败清单（failed_pages）是 reason 的权威来源。
// 缺页记录 = 还没跑到（pending），不区分「识别中」与「未开始」——那是文件名行的事。

export type PageCellState = 'success' | 'failed' | 'pending';
export type ItemRecognitionState = 'not_started' | 'running' | 'done' | 'failed' | 'cancelled';

export type ItemPageCell = {
  page: number;
  state: PageCellState;
  reason?: string | null;
  duration_ms?: number | null;
  cache_hit?: boolean;
};

export type ItemPageStatus = {
  item_id: string;
  job_id: string;
  file_id: string;
  /** false = 该文件类型不支持页级视图/页级导出（docx/txt/md…）。 */
  paged: boolean;
  page_count: number;
  recognition_state: ItemRecognitionState;
  snapshot_at: string;
  pages: ItemPageCell[];
  failed_pages: number[];
};

export type BatchExportVolume = {
  name: string;
  size_bytes?: number;
  file_count?: number;
};

export type PartialExportTask = {
  export_id: string;
  kind: string;
  title: string;
  status: string;
  error?: string | null;
  progress?: { stage?: string; current?: number; total?: number };
  volumes: BatchExportVolume[];
  total_bytes?: number;
  file_count?: number;
  created_at: string;
  finished_at?: string | null;
  job_id?: string;
  item_id?: string;
  pages?: number[];
  page_count?: number;
  excluded_failed_pages?: number[];
  snapshot_at?: string;
};

const PAGE_CELL_STATES: ReadonlySet<string> = new Set(['success', 'failed', 'pending']);

function normalizePageCell(raw: unknown, fallbackPage: number): ItemPageCell {
  const cell = asRecord(raw);
  const state = asString(cell.state, 'pending');
  return {
    page: asNumber(cell.page, fallbackPage),
    state: (PAGE_CELL_STATES.has(state) ? state : 'pending') as PageCellState,
    reason: cell.reason == null ? null : asString(cell.reason),
    duration_ms: cell.duration_ms == null ? null : asNumber(cell.duration_ms),
    cache_hit: cell.cache_hit === true,
  };
}

export function normalizeItemPageStatus(raw: unknown): ItemPageStatus {
  const payload = asRecord(raw);
  const pages = Array.isArray(payload.pages)
    ? payload.pages.map((cell, index) => normalizePageCell(cell, index + 1))
    : [];
  return {
    item_id: asString(payload.item_id),
    job_id: asString(payload.job_id),
    file_id: asString(payload.file_id),
    paged: payload.paged === true,
    page_count: asNumber(payload.page_count, pages.length),
    recognition_state: asString(
      payload.recognition_state,
      'not_started',
    ) as ItemRecognitionState,
    snapshot_at: asString(payload.snapshot_at),
    pages,
    failed_pages: Array.isArray(payload.failed_pages)
      ? payload.failed_pages.map((value) => asNumber(value)).filter((value) => value > 0)
      : [],
  };
}

export function normalizePartialExportTask(raw: unknown): PartialExportTask {
  const task = asRecord(raw);
  const progress = asRecord(task.progress);
  const volumes = Array.isArray(task.volumes)
    ? task.volumes.map((volume) => {
        const record = asRecord(volume);
        return {
          name: asString(record.name),
          size_bytes: asNumber(record.size_bytes),
          file_count: asNumber(record.file_count),
        };
      })
    : [];
  const pages = Array.isArray(task.pages)
    ? task.pages.map((value) => asNumber(value)).filter((value) => value > 0)
    : [];
  const excluded = Array.isArray(task.excluded_failed_pages)
    ? task.excluded_failed_pages.map((value) => asNumber(value)).filter((value) => value > 0)
    : [];
  return {
    export_id: asString(task.export_id),
    kind: asString(task.kind, 'partial_redaction'),
    title: asString(task.title),
    status: asString(task.status, 'queued'),
    error: task.error == null ? null : asString(task.error),
    progress: {
      stage: asString(progress.stage),
      current: asNumber(progress.current),
      total: asNumber(progress.total),
    },
    volumes,
    total_bytes: asNumber(task.total_bytes),
    file_count: asNumber(task.file_count),
    created_at: asString(task.created_at),
    finished_at: task.finished_at == null ? null : asString(task.finished_at),
    job_id: asString(task.job_id),
    item_id: asString(task.item_id),
    pages,
    page_count: asNumber(task.page_count),
    excluded_failed_pages: excluded,
    snapshot_at: asString(task.snapshot_at),
  };
}

/** 页格三态投影。识别进行中也可以调（每页状态是快照）。 */
export function getItemPageStatus(jobId: string, itemId: string): Promise<ItemPageStatus> {
  return get(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/page-status`,
  ).then(normalizeItemPageStatus);
}

/**
 * 部分导出：只导出选中页（原卷页号）+ 一张说明页。
 *
 * `acknowledgeFailedExcluded` = 用户已确认「区间内的失败页会被排除」——后端在
 * 区间含失败页且未确认时返回 409 failed_pages_ack_required。
 */
export function startPartialExport(
  jobId: string,
  itemId: string,
  pages: number[],
  acknowledgeFailedExcluded = false,
): Promise<PartialExportTask> {
  return post(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/partial-export`,
    { pages, acknowledge_failed_excluded: acknowledgeFailedExcluded },
  ).then(normalizePartialExportTask);
}

export function getPartialExport(exportId: string): Promise<PartialExportTask> {
  return get(`/files/batch/export/${encodeURIComponent(exportId)}`).then(normalizePartialExportTask);
}

export async function listItemExports(
  jobId: string,
  itemId: string,
): Promise<{ exports: PartialExportTask[]; unavailable: string[] }> {
  const payload = asRecord(
    await get(`/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/exports`),
  );
  const exports = Array.isArray(payload.exports)
    ? payload.exports.map(normalizePartialExportTask)
    : [];
  return { exports, unavailable: asStringArray(payload.unavailable) };
}

/** Issue#257 能力 2：单页重试的受理结果（后端 202）。 */
export interface PageRetryResult {
  queued_pages: number[];
}

/** 只重跑这些失败页（后端会先落 ``page_retry_pages`` 标记再投递任务）。 */
export function retryItemPages(
  jobId: string,
  itemId: string,
  pages: number[],
): Promise<PageRetryResult> {
  return post(`/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/retry-pages`, {
    pages,
  }).then((payload) => {
    const record = asRecord(payload);
    return {
      queued_pages: Array.isArray(record.queued_pages)
        ? record.queued_pages.map((value) => asNumber(value)).filter((value) => value > 0)
        : [],
    };
  });
}
