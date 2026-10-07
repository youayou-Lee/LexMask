// Copyright 2026 LexMask Contributors

import { authFetch } from '@/services/api-client';
import { t } from '@/i18n';
import type { Decision, MappingItemDto } from './lib/agent-md-flow';

/** Issue#75 /agent-md 后端 HTTP 契约的前端 API client（fetch 走 authFetch 自动带 cookie+CSRF）。 */

/** GET /agent-md/{id}/status 的返回体。 */
export interface AgentMdStatus {
  task_id: string;
  state: string;
  stage: string;
  pages_done: number;
  pages_total: number;
  message: string;
  warnings_count: number;
}

/** POST /agent-md/{id}/confirm 的返回体。 */
export interface ConfirmResult {
  output_file_id: string;
  downloads: { md: string; mapping: string; retained: string };
}

/**
 * 读取后端错误信封（AppError：{error_code, message, detail, request_id}）里的
 * 用户可读文案，取不到时回退 i18n key（key 由页面任务注册；缺 key 时 t() 原样返回 key）。
 * 与 playground 同款实现：detail 为 dict 时跳过、响应体非 JSON 时走回退。
 */
async function responseErrorMessage(res: Response, fallbackKey: string): Promise<string> {
  try {
    const data = (await res.json()) as { detail?: unknown; message?: unknown; error?: unknown };
    const detail = data.detail ?? data.message ?? data.error;
    if (typeof detail === 'string' && detail.trim()) return detail;
  } catch {
    // Keep the localized fallback when the response body is not JSON.
  }
  return t(fallbackKey);
}

async function readError(res: Response, fallbackKey: string): Promise<never> {
  throw new Error(await responseErrorMessage(res, fallbackKey));
}

export const agentMdApi = {
  async upload(file: File, password?: string): Promise<{ task_id: string }> {
    const fd = new FormData();
    fd.append('file', file);
    if (password) fd.append('password', password);
    const res = await authFetch('/api/v1/agent-md/upload', { method: 'POST', body: fd });
    if (!res.ok) await readError(res, 'agentMd.uploadFailed');
    return res.json();
  },

  async status(taskId: string): Promise<AgentMdStatus> {
    const res = await authFetch(`/api/v1/agent-md/${taskId}/status`);
    if (!res.ok) await readError(res, 'agentMd.statusFailed');
    return res.json();
  },

  async mapping(taskId: string): Promise<{ items: MappingItemDto[] }> {
    const res = await authFetch(`/api/v1/agent-md/${taskId}/mapping`);
    if (!res.ok) await readError(res, 'agentMd.mappingFailed');
    return res.json();
  },

  async confirm(taskId: string, decisions: Decision[]): Promise<ConfirmResult> {
    const res = await authFetch(`/api/v1/agent-md/${taskId}/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decisions }),
    });
    if (!res.ok) await readError(res, 'agentMd.confirmFailed');
    return res.json();
  },

  /** 产物下载地址（配合 downloadFile/authenticatedBlobUrl 携带凭据使用）。 */
  artifactUrl(taskId: string, kind: 'md' | 'mapping' | 'retained'): string {
    return `/api/v1/agent-md/${taskId}/artifacts/${kind}`;
  },
};
