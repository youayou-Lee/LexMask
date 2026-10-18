// Copyright 2026 LexMask Contributors

import { beforeEach, describe, expect, it, vi } from 'vitest';

import { SINGLE_TASK_POLL_MS } from '@/constants/timing';
import { ApiError, apiClient } from './api-client';
import {
  listActiveSingleJobs,
  resolveAwaitingReviewTask,
  submitRecognitionTask,
  submitRedactionTask,
} from './singleTaskApi';

// Issue#194 T7：单文件任务前端 API 层。mock 层沿用仓内部分 mock 风格
// （utils.vision-page-concurrency.test.ts）：保留 api-client 其余导出，
// 只替换 get/post ⇒ 断言链路经过真实的 URL/参数映射代码。

vi.mock('./api-client', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api-client')>();
  return {
    ...actual,
    get: vi.fn(),
    post: vi.fn(),
  };
});

import { get, post } from './api-client';
import { jobTypeForNav } from '@/utils/jobPrimaryNavigation';

const mockedPost = vi.mocked(post);
const mockedGet = vi.mocked(get);

/** 后端统一错误信封（backend/app/core/errors.py error_response）。 */
function envelope(
  statusCode: number,
  errorCode: string,
  message: string,
): { response: { status: number; data: Record<string, unknown> }; message: string } {
  return {
    response: {
      status: statusCode,
      data: { error_code: errorCode, message, detail: {}, request_id: 'req-test' },
    },
    message: `Request failed with status code ${statusCode}`,
  };
}

describe('Issue#194 T7：submitRecognitionTask', () => {
  beforeEach(() => {
    mockedPost.mockReset();
  });

  it('POST /files/{fileId}/recognition-task，body 带 entity_type_ids，回执三元组原样返回', async () => {
    const ref = { job_id: 'job-1', item_id: 'item-1', file_id: 'file-1' };
    mockedPost.mockResolvedValueOnce(ref);

    await expect(submitRecognitionTask('file-1', ['PER_NAME', 'ORG_NAME'])).resolves.toEqual(ref);

    expect(mockedPost).toHaveBeenCalledTimes(1);
    expect(mockedPost).toHaveBeenCalledWith(
      '/files/file-1/recognition-task',
      { entity_type_ids: ['PER_NAME', 'ORG_NAME'] },
    );
  });

  it('空 entity_type_ids 也原样发空列表（后端 schema 允许：交给管道按默认实体类型处理）', async () => {
    mockedPost.mockResolvedValueOnce({ job_id: 'j', item_id: 'i', file_id: 'f' });

    await submitRecognitionTask('file-2', []);

    expect(mockedPost).toHaveBeenCalledWith('/files/file-2/recognition-task', {
      entity_type_ids: [],
    });
  });

  it('fileId 中的特殊字符经 encodeURIComponent（钉 URL 组装方式）', async () => {
    mockedPost.mockResolvedValueOnce({ job_id: 'j', item_id: 'i', file_id: 'f' });

    await submitRecognitionTask('a/b c', []);

    expect(mockedPost).toHaveBeenCalledWith('/files/a%2Fb%20c/recognition-task', {
      entity_type_ids: [],
    });
  });
});

describe('Issue#194 T7：submitRedactionTask', () => {
  beforeEach(() => {
    mockedPost.mockReset();
  });

  it('POST /jobs/{jobId}/items/{itemId}/redact，body（entities/bounding_boxes/config）逐字透传', async () => {
    const ref = { job_id: 'job-9', item_id: 'item-9', file_id: 'file-9' };
    mockedPost.mockResolvedValueOnce(ref);
    const body = {
      entities: [{ id: 'e1', label: 'PER_NAME', text: '张三' }],
      bounding_boxes: [{ page: 1, x0: 1, y0: 2, x1: 3, y1: 4 }],
      config: {
        replacement_mode: 'pseudonym' as const,
        custom_replacements: { 张三: '李四' },
        watermark_text: '内部资料',
      },
    };

    await expect(submitRedactionTask('job-9', 'item-9', body)).resolves.toEqual(ref);

    expect(mockedPost).toHaveBeenCalledTimes(1);
    expect(mockedPost).toHaveBeenCalledWith('/jobs/job-9/items/item-9/redact', body);
  });

  it('jobId/itemId 经 encodeURIComponent', async () => {
    mockedPost.mockResolvedValueOnce({ job_id: 'j', item_id: 'i', file_id: 'f' });

    await submitRedactionTask('j/1', 'i/2', {
      entities: [],
      bounding_boxes: [],
      config: { replacement_mode: 'mask' },
    });

    expect(mockedPost).toHaveBeenCalledWith('/jobs/j%2F1/items/i%2F2/redact', {
      entities: [],
      bounding_boxes: [],
      config: { replacement_mode: 'mask' },
    });
  });
});

describe('Issue#194 T7：错误通道（沿用 api-client，不吞不裹）', () => {
  beforeEach(() => {
    mockedPost.mockReset();
  });

  it('409 冲突：ApiError 原样透传 —— status 409 + 后端 detail 文案可直接展示', async () => {
    const conflict = new ApiError('有文件正在处理：a.pdf，请等它完成', 409, 'server');
    mockedPost.mockRejectedValueOnce(conflict);

    await expect(submitRecognitionTask('file-1', [])).rejects.toBe(conflict);
  });

  it('加密 PDF 400：ApiError.errorCode === PDF_ENCRYPTED_NEEDS_PASSWORD 供调用方分支', async () => {
    const encrypted = new ApiError(
      '该 PDF 已加密，请输入密码后重试',
      400,
      'server',
      'PDF_ENCRYPTED_NEEDS_PASSWORD',
    );
    mockedPost.mockRejectedValueOnce(encrypted);

    await expect(submitRedactionTask('job-1', 'item-1', {
      entities: [],
      bounding_boxes: [],
      config: { replacement_mode: 'mask' },
    })).rejects.toMatchObject({
      status: 400,
      errorCode: 'PDF_ENCRYPTED_NEEDS_PASSWORD',
      message: '该 PDF 已加密，请输入密码后重试',
    });
  });

  it('接线判据：真实 api-client 拦截器把统一信封的 error_code 带上 ApiError（409）', async () => {
    const originalAdapter = apiClient.defaults.adapter;
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      apiClient.defaults.adapter = async () => {
        throw envelope(409, 'HTTP_409', '有文件正在处理：a.pdf，请等它完成');
      };
      await expect(apiClient.post('/files/file-1/recognition-task', {})).rejects.toMatchObject({
        name: 'ApiError',
        status: 409,
        errorType: 'server',
        message: '有文件正在处理：a.pdf，请等它完成',
      });
    } finally {
      apiClient.defaults.adapter = originalAdapter;
      consoleSpy.mockRestore();
    }
  });

  it('接线判据：真实 api-client 拦截器对加密 PDF 400 信封透出 errorCode', async () => {
    const originalAdapter = apiClient.defaults.adapter;
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    try {
      apiClient.defaults.adapter = async () => {
        throw envelope(400, 'PDF_ENCRYPTED_NEEDS_PASSWORD', '该 PDF 已加密，请输入密码后重试');
      };
      await expect(apiClient.post('/files/file-1/recognition-task', {})).rejects.toMatchObject({
        name: 'ApiError',
        status: 400,
        errorCode: 'PDF_ENCRYPTED_NEEDS_PASSWORD',
        message: '该 PDF 已加密，请输入密码后重试',
      });
    } finally {
      apiClient.defaults.adapter = originalAdapter;
      consoleSpy.mockRestore();
    }
  });
});

describe('Issue#194 T7：listActiveSingleJobs', () => {
  beforeEach(() => {
    mockedGet.mockReset();
  });

  it('薄封装：真实 listJobs 收到 GET /jobs?job_type=single&status=active，返回 .jobs', async () => {
    mockedGet.mockResolvedValueOnce({
      jobs: [
        {
          id: 'job-single-1',
          job_type: 'single',
          title: 'a.pdf',
          status: 'processing',
          skip_item_review: false,
          config: {},
          created_at: '2026-10-18T00:00:00Z',
          updated_at: '2026-10-18T00:00:00Z',
          progress: {},
        },
        {
          id: 'job-text-1',
          job_type: 'text_batch',
          title: 'batch',
          status: 'processing',
          skip_item_review: false,
          config: {},
          created_at: '2026-10-18T00:00:00Z',
          updated_at: '2026-10-18T00:00:00Z',
          progress: {},
        },
      ],
      total: 2,
      page: 1,
      page_size: 20,
      stats: {},
    });

    const jobs = await listActiveSingleJobs();

    expect(mockedGet).toHaveBeenCalledTimes(1);
    expect(mockedGet).toHaveBeenCalledWith('/jobs', {
      params: { job_type: 'single', status: 'active' },
    });
    expect(jobs).toHaveLength(2);
    // 判别点：后端回 'single' 时 normalize 不得把它吞成 'text_batch'。
    expect(jobs[0].job_type).toBe('single');
    expect(jobs[1].job_type).toBe('text_batch');
  });

  it('空列表透传（无活动单文件任务时不造数据）', async () => {
    mockedGet.mockResolvedValueOnce({
      jobs: [],
      total: 0,
      page: 1,
      page_size: 20,
      stats: {},
    });

    await expect(listActiveSingleJobs()).resolves.toEqual([]);
  });
});

describe('Issue#194 Bug#2：resolveAwaitingReviewTask（刷新后 awaiting_review 兜底解析）', () => {
  beforeEach(() => {
    mockedGet.mockReset();
    mockedPost.mockReset();
  });

  function awaitingSummary(config: Record<string, unknown> = { single_file_id: 'f1' }) {
    return {
      id: 'j-9',
      job_type: 'single',
      title: 'a.pdf',
      status: 'awaiting_review',
      skip_item_review: false,
      config,
      created_at: '2026-10-18T00:00:00Z',
      updated_at: '2026-10-18T00:00:00Z',
      progress: {},
    };
  }

  it('命中：GET /jobs?job_type=single&status=awaiting_review 按 config.single_file_id 找 job，再 batch-details 取 items[0].id 作处理项', async () => {
    mockedGet.mockResolvedValueOnce({
      jobs: [awaitingSummary()],
      total: 1,
      page: 1,
      page_size: 20,
      stats: {},
    });
    mockedPost.mockResolvedValueOnce({
      jobs: [
        {
          ...awaitingSummary(),
          items: [
            {
              id: 'it-9',
              job_id: 'j-9',
              file_id: 'f1',
              sort_order: 0,
              status: 'awaiting_review',
              created_at: '2026-10-18T00:00:00Z',
              updated_at: '2026-10-18T00:00:00Z',
            },
          ],
        },
      ],
    });

    await expect(resolveAwaitingReviewTask('f1')).resolves.toEqual({
      job_id: 'j-9',
      item_id: 'it-9',
      file_id: 'f1',
    });

    expect(mockedGet).toHaveBeenCalledTimes(1);
    expect(mockedGet).toHaveBeenCalledWith('/jobs', {
      params: { job_type: 'single', status: 'awaiting_review' },
    });
    expect(mockedPost).toHaveBeenCalledTimes(1);
    expect(mockedPost).toHaveBeenCalledWith('/jobs/batch-details', { ids: ['j-9'] });
  });

  it('清单里没有 config.single_file_id 匹配的 job → null，且不发 batch-details', async () => {
    mockedGet.mockResolvedValueOnce({
      jobs: [awaitingSummary({ single_file_id: 'other-file' })],
      total: 1,
      page: 1,
      page_size: 20,
      stats: {},
    });

    await expect(resolveAwaitingReviewTask('f1')).resolves.toBeNull();
    expect(mockedPost).not.toHaveBeenCalled();
  });

  it('job 命中但 batch 详情无 items（异常形态）→ null，不造句柄', async () => {
    mockedGet.mockResolvedValueOnce({
      jobs: [awaitingSummary()],
      total: 1,
      page: 1,
      page_size: 20,
      stats: {},
    });
    mockedPost.mockResolvedValueOnce({ jobs: [] });

    await expect(resolveAwaitingReviewTask('f1')).resolves.toBeNull();
  });
});

describe('Issue#194 T7：轮询常量', () => {
  it('SINGLE_TASK_POLL_MS = 1500（T8-T12 轮询按此节拍）', () => {
    expect(SINGLE_TASK_POLL_MS).toBe(1500);
  });
});

describe('Issue#194 T7：jobTypeForNav 导航边界（T8-T12 接管前的行为钉）', () => {
  it('四种批量类型恒等映射（现状零漂移）', () => {
    expect(jobTypeForNav('text_batch')).toBe('text_batch');
    expect(jobTypeForNav('image_batch')).toBe('image_batch');
    expect(jobTypeForNav('smart_batch')).toBe('smart_batch');
    expect(jobTypeForNav('structured_batch')).toBe('structured_batch');
  });

  it("'single' 显式落到 text_batch（沿用 normalizeJobType 的既有归一行为，单文件导航由 T8-T12 收口）", () => {
    expect(jobTypeForNav('single')).toBe('text_batch');
  });
});
