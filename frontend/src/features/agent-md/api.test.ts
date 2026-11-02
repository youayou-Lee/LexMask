// Copyright 2026 LexMask Contributors

// Issue#212 门⑥ I4（F1）：confirm 的 ack 载荷必须在**网络层**被钉住——
// 只测页面 onConfirm 参数时可以删掉这一跳仍全绿。

import { afterEach, describe, expect, it, vi } from 'vitest';

const authFetch = vi.fn();
vi.mock('@/services/api-client', () => ({
  authFetch: (...args: unknown[]) => authFetch(...args),
  getCsrfToken: () => 'csrf',
}));

import { agentMdApi } from './api';

afterEach(() => authFetch.mockReset());

function okJson(body: unknown) {
  return Promise.resolve({ ok: true, json: () => Promise.resolve(body) } as Response);
}

const RESULT = {
  output_file_id: 'of-1',
  downloads: { md: '/m', mapping: '/p', retained: '/r' },
};

describe('agentMdApi.confirm 零实体 ack（Issue#212）', () => {
  it('options.acknowledgeZeroEntities=true ⇒ body 带 acknowledge_zero_entities: true', async () => {
    authFetch.mockReturnValue(okJson(RESULT));

    await agentMdApi.confirm('tk-1', [], { acknowledgeZeroEntities: true });

    const [, init] = authFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({
      decisions: [],
      acknowledge_zero_entities: true,
    });
  });

  it('未传 options ⇒ body 不含 ack 字段（防"永远 ack"假绿）', async () => {
    authFetch.mockReturnValue(okJson(RESULT));

    await agentMdApi.confirm('tk-1', []);

    const [, init] = authFetch.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ decisions: [] });
  });
});
