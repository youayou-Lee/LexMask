// Copyright 2026 LexMask Contributors

import { authFetch, getCsrfToken } from '@/services/api-client';
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

/** 上传进度回调载荷（loadedBytes/totalBytes 为 XHR 原始字节计数）。 */
export interface UploadProgress {
  loadedBytes: number;
  totalBytes: number;
  percent: number;
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

/**
 * 解析非 2xx 响应体里的错误信封（AppError：{error_code, message, detail}）。
 * XHR 与 fetch 两条上传路径共用，保证 PDF_ENCRYPTED_NEEDS_PASSWORD / PDF_WRONG_PASSWORD 分支一致。
 */
export function parseUploadErrorEnvelope(
  bodyText: string,
): { code: string | null; message: string | null } {
  try {
    const data = JSON.parse(bodyText) as { error_code?: unknown; message?: unknown; detail?: unknown };
    const code = typeof data.error_code === 'string' && data.error_code ? data.error_code : null;
    const detail = data.message ?? data.detail;
    const message = typeof detail === 'string' && detail.trim() ? detail.trim() : null;
    return { code, message };
  } catch {
    return { code: null, message: null };
  }
}

/** 上传失败错误：携带后端 error_code（PDF_ENCRYPTED_NEEDS_PASSWORD 等）供调用方分支。 */
export class UploadError extends Error {
  code: string | null;
  constructor(message: string, code: string | null) {
    super(message);
    this.name = 'UploadError';
    this.code = code;
  }
}

/**
 * XHR 版上传：fetch 拿不到上传方向进度，196MB 大文件会全程黑盒，
 * 改用 XMLHttpRequest.upload.onprogress 上报真实字节进度（同源 cookie 自动携带）。
 */
export function uploadWithProgress(
  file: File,
  password: string | undefined,
  onProgress: (progress: UploadProgress) => void,
): Promise<{ task_id: string }> {
  return new Promise((resolve, reject) => {
    const fd = new FormData();
    fd.append('file', file);
    if (password) fd.append('password', password);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/v1/agent-md/upload');
    xhr.withCredentials = true;
    const csrfToken = getCsrfToken();
    if (csrfToken) xhr.setRequestHeader('X-CSRF-Token', csrfToken);

    xhr.upload.onprogress = (event) => {
      const totalBytes = event.total || file.size;
      const loadedBytes = event.loaded;
      const percent = totalBytes > 0 ? Math.min(100, Math.round((loadedBytes / totalBytes) * 100)) : 0;
      onProgress({ loadedBytes, totalBytes, percent });
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText) as { task_id: string });
        } catch {
          reject(new Error(t('agentMd.uploadFailed')));
        }
        return;
      }
      const { code, message } = parseUploadErrorEnvelope(xhr.responseText);
      reject(new UploadError(message || t('agentMd.uploadFailed'), code));
    };
    xhr.onerror = () => reject(new Error(t('agentMd.uploadFailed')));
    xhr.ontimeout = () => reject(new Error(t('agentMd.uploadFailed')));
    xhr.send(fd);
  });
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

  /**
   * 复用 file_store 已上传文件建任务（playground 分流入口，免重复上传）。
   * 后端按 file_id 查 file_store 的 file_path，须为 UPLOAD_DIR 内存在的 PDF。
   */
  async startFromFileId(fileId: string, filename?: string): Promise<{ task_id: string }> {
    const fd = new FormData();
    fd.append('file_id', fileId);
    if (filename) fd.append('filename', filename);
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
