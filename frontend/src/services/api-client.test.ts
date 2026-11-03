// Copyright 2026 LexMask Contributors

import { afterEach, describe, expect, it, vi } from 'vitest';

import { ApiError, fetchBlob } from './api-client';

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchBlob error envelope (#232 M-3)', () => {
  it('surfaces the backend message instead of "{}" when detail is an empty object', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ error_code: 'X', message: '文件已删除', detail: {} }, 404)),
    );
    await expect(fetchBlob('/api/v1/files/1/download')).rejects.toThrow('文件已删除');
  });

  it('falls back to the HTTP status line when the envelope carries no text', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => jsonResponse({ error_code: 'X', message: '', detail: {} }, 400)),
    );
    await expect(fetchBlob('/api/v1/files/1/download')).rejects.toThrow('HTTP 400');
  });

  it('carries the machine-readable code and structured detail on ApiError (#232 I-1)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          {
            error_code: 'FILE_CONTENT_MISMATCH',
            message: '不匹配',
            detail: { detected_format: 'pdf', extension: '.doc' },
          },
          400,
        ),
      ),
    );
    const err: unknown = await fetchBlob('/api/v1/files/1/download').catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).errorCode).toBe('FILE_CONTENT_MISMATCH');
    expect((err as ApiError).detail).toEqual({ detected_format: 'pdf', extension: '.doc' });
  });
});
