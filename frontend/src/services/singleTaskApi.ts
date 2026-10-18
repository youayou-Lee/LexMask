// Copyright 2026 LexMask Contributors

import { post } from './api-client';
import { listJobs, type JobSummary } from './jobsApi';

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

/** 单文件识别：把已上传文件转后台任务（409 = 账号下已有活动任务）。 */
export function submitRecognitionTask(
  fileId: string,
  entityTypeIds: string[],
): Promise<SingleTaskRef> {
  return post<SingleTaskRef>(`/files/${encodeURIComponent(fileId)}/recognition-task`, {
    entity_type_ids: entityTypeIds,
  });
}

/** 单文件确认后打码：前端调整后的实体 seed + 展示配置（打码工人按既有键读取）。 */
export function submitRedactionTask(
  jobId: string,
  itemId: string,
  body: {
    entities: Array<Record<string, unknown>>;
    bounding_boxes: Array<Record<string, unknown>>;
    config: {
      replacement_mode: 'mask' | 'pseudonym';
      custom_replacements?: Record<string, string>;
      watermark_text?: string;
    };
  },
): Promise<SingleTaskRef> {
  return post<SingleTaskRef>(
    `/jobs/${encodeURIComponent(jobId)}/items/${encodeURIComponent(itemId)}/redact`,
    body,
  );
}

/** 活动中的单文件任务（守门冲突提示 / 断点恢复轮询用）。 */
export function listActiveSingleJobs(): Promise<JobSummary[]> {
  return listJobs({ job_type: 'single', status: 'active' }).then((response) => response.jobs);
}
