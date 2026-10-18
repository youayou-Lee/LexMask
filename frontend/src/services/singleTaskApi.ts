// Copyright 2026 LexMask Contributors

import { post } from './api-client';
import { getJobsBatch, listJobs, type JobSummary } from './jobsApi';

/**
 * Issue#194 单文件任务的前端 API 层。
 *
 * 后端契约（backend/app/api/files.py + jobs.py）：
 * - POST /files/{file_id}/recognition-task  → {job_id, item_id, file_id}
 * - POST /jobs/{job_id}/items/{item_id}/redact → {job_id, item_id, status}
 * - GET  /jobs?job_type=single&status=active → 活动中的单文件任务
 *
 * 错误通道沿用 api-client 的 ApiError：非 2xx 时 message 即后端统一信封
 * （{error_code, message, detail}）的文案 —— 409 冲突文案
 * 「有文件正在处理：<文件名>，请等它完成」可直接展示；加密 PDF 400 的
 * `errorCode === 'PDF_ENCRYPTED_NEEDS_PASSWORD'` 供调用方弹密码框分支。
 */

/** 受理回执：后台 job / item / file 三元组（与后端 SingleTaskRef 同形）。 */
export type SingleTaskRef = { job_id: string; item_id: string; file_id: string };

/**
 * 打码受理回执（与后端 SingleRedactResult 同形）：只有 job_id/item_id/status。
 * Controller 交接约束：回执无 file_id——fileId 一律以调用方现有状态为准，不得从打码回执读。
 */
export type SingleRedactReceipt = { job_id: string; item_id: string; status: string };

/** 单文件识别：把已上传文件转后台任务（409 = 账号下已有活动任务）。 */
export function submitRecognitionTask(
  fileId: string,
  entityTypeIds: string[],
): Promise<SingleTaskRef> {
  return post<SingleTaskRef>(`/files/${encodeURIComponent(fileId)}/recognition-task`, {
    entity_type_ids: entityTypeIds,
  });
}

/**
 * 单文件确认后打码：前端调整后的实体 seed + 展示配置（打码工人按既有键读取）。
 * entities/bounding_boxes 按不透明载荷透传（序列化层不解释键，形状归后端工人所有），
 * 调用方传前端 Entity/BoundingBox 域对象即可。
 */
export function submitRedactionTask(
  jobId: string,
  itemId: string,
  body: {
    entities: unknown[];
    bounding_boxes: unknown[];
    config: {
      replacement_mode: 'mask' | 'pseudonym';
      custom_replacements?: Record<string, string>;
      watermark_text?: string;
    };
  },
): Promise<SingleRedactReceipt> {
  return post<SingleRedactReceipt>(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/redact`,
    body,
  );
}

/** 活动中的单文件任务（守门冲突提示 / 断点恢复轮询用）。 */
export function listActiveSingleJobs(): Promise<JobSummary[]> {
  return listJobs({ job_type: 'single', status: 'active' }).then((response) => response.jobs);
}

/**
 * 刷新后兜底解析 awaiting_review 态的单文件任务句柄（Issue#194 Bug#2）。
 *
 * provider 的挂载恢复只认领 active 任务（listActiveSingleJobs）；识别完成停稳在
 * awaiting_review 的任务刷新后不在账本里（watch 未注册），taskForFile 必 miss。
 * 这里按 fileId 直查后端任务清单：`config.single_file_id === fileId` 的 single
 * job → batch 详情取 `items[0].id` 作处理项（单文件任务恒一件）。查无返回 null，
 * 由调用方决定报错；网络错误原样上抛（外层统一 toast 后端文案）。
 */
export async function resolveAwaitingReviewTask(fileId: string): Promise<SingleTaskRef | null> {
  const response = await listJobs({ job_type: 'single', status: 'awaiting_review' });
  const job = response.jobs.find((candidate) => candidate.config.single_file_id === fileId);
  if (!job) return null;
  const { jobs } = await getJobsBatch([job.id]);
  const item = jobs[0]?.items[0];
  if (!item) return null;
  return { job_id: job.id, item_id: item.id, file_id: fileId };
}
