// Copyright 2026 LexMask Contributors

import type { JobDetail } from '@/services/jobsApi';

/**
 * Issue#194 单文件任务的前端 phase 模型（纯函数，便于单测）。
 *
 * 后端状态语义（backend T2-T5）：
 * - 识别中 job=processing（item parsing/ner/vision）→ 完成停 job/item=awaiting_review；
 * - 打码提交后 job 又回 processing（item review_approved→redacting 由工人推进）→ completed；
 * - failed / cancelled（cancelled 视同 failed）。
 * job=processing 单凭 job 状态分不出识别段/打码段，必须看 item 状态；
 * 列表 summary（挂载恢复基线）拿不到 item，一律按识别段处理。
 */

export type SingleTaskPhase =
  | 'recognizing'
  | 'awaiting_review'
  | 'redacting'
  | 'done'
  | 'failed';

export type SingleTaskView = {
  jobId: string;
  itemId: string;
  fileId: string;
  filename: string;
  phase: SingleTaskPhase;
  current: number;
  total: number;
  message: string | null;
  error: string | null;
};

/** job/item 状态 → phase。itemStatus 未知（列表 summary 基线）传 null，按识别段处理。 */
export function deriveSingleTaskPhase(
  jobStatus: string,
  itemStatus: string | null,
): SingleTaskPhase {
  switch (jobStatus) {
    case 'awaiting_review':
      return 'awaiting_review';
    case 'completed':
      return 'done';
    case 'failed':
    case 'cancelled':
      return 'failed';
    default:
      // queued/running/processing：item 状态可区分识别段与打码段。
      if (itemStatus === 'review_approved' || itemStatus === 'redacting') return 'redacting';
      return 'recognizing';
  }
}

/** 终态：停止轮询，短留 SINGLE_TASK_DONE_RETAIN_MS 后从账本清除。 */
export function isTerminalPhase(phase: SingleTaskPhase): boolean {
  return phase === 'done' || phase === 'failed';
}

/** 阶段迁移 → 通知文案 i18n 键；无通知的迁移（recognizing/redacting 之间）返回 null。 */
export function notifyKeyForPhase(phase: SingleTaskPhase): string | null {
  switch (phase) {
    case 'awaiting_review':
      return 'singleTask.notify.recognized';
    case 'done':
      return 'singleTask.notify.redacted';
    case 'failed':
      return 'singleTask.notify.failed';
    default:
      return null;
  }
}

/** 由单文件 job 详情构建视图（item 取首个——单文件任务恒一件）。 */
export function buildSingleTaskView(params: {
  jobId: string;
  fileId: string;
  filename: string;
  detail: JobDetail;
}): SingleTaskView {
  const { detail } = params;
  const item = detail.items[0];
  return {
    jobId: params.jobId,
    itemId: item?.id ?? '',
    fileId: params.fileId || item?.file_id || '',
    filename: params.filename || detail.title || item?.filename || '',
    phase: deriveSingleTaskPhase(detail.status, item?.status ?? null),
    current: item?.progress_current ?? 0,
    total: item?.progress_total ?? 0,
    message: item?.progress_message ?? null,
    error: item?.error_message ?? detail.error_message ?? null,
  };
}
