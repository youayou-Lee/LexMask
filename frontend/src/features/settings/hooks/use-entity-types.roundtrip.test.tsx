// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#267 A7（F1 拍板钉）：types 目录 hook 在面板下沉（Radix inactive 卸载）后
// 「每次进入 types 页签都重发目录请求 + loading spinner」是有意拍板的行为（方案 v2 §2.1）
// ——兄弟页签（redaction/words）同语义，新鲜度更好，页签栏兜底白等。
// 本钉把拍板固化：卸载重挂后 fetch 必须重发且 loading 重新为 true。
// 未来有人「顺手」加模块级缓存或改 loading 条件时此钉转红、强制重审。
import { afterEach, describe, expect, it, vi } from 'vitest';
import { renderHook, waitFor } from '@testing-library/react';
import { useEntityTypes } from './use-entity-types';
import type { EntityTypeConfig } from '../hooks/use-entity-types';

const fetchTypesMock = vi.hoisted(() => vi.fn());
const fetchPipelinesMock = vi.hoisted(() => vi.fn());
const authFetchMock = vi.hoisted(() => vi.fn());

vi.mock('@/services/recognition-config', () => ({
  fetchRecognitionEntityTypes: fetchTypesMock,
  fetchRecognitionPipelines: fetchPipelinesMock,
  // Issue#267 门⑥ M-2：常量权威定义下沉服务层后，消费侧 import 此键——
  // mock 工厂必须镜像真实模块的导出面（缺键会使消费侧在 try 内抛错、
  // 走 catch 分支把 loading 提前落 false，A7 首挂断言即红）。
  RECOGNITION_FETCH_TIMEOUT_MS: 25_000,
}));
vi.mock('@/services/api-client', () => ({
  authFetch: authFetchMock,
}));

const ONE_TYPE: EntityTypeConfig[] = [
  { id: 'PERSON', name: 'Person', color: '#000000', enabled: true },
];

afterEach(() => {
  fetchTypesMock.mockReset();
  fetchPipelinesMock.mockReset();
  authFetchMock.mockReset();
});

describe('Issue#267：types 目录 hook 再进入重取（F1 拍板钉）', () => {
  it('A7：首次挂载 loading=true→落定 false；卸载重挂 → fetch 重发且 loading 重新为 true', async () => {
    fetchTypesMock.mockResolvedValue(ONE_TYPE);
    authFetchMock.mockResolvedValue({ ok: true, json: async () => ({ domains: [] }) });
    fetchPipelinesMock.mockResolvedValue([]);

    const first = renderHook(() => useEntityTypes());
    expect(first.result.current.loading).toBe(true);
    await waitFor(() => expect(first.result.current.loading).toBe(false));
    expect(first.result.current.entityTypes).toHaveLength(1);
    expect(fetchTypesMock).toHaveBeenCalledTimes(1);
    first.unmount();

    const second = renderHook(() => useEntityTypes());
    // 再进入 = 重取 + 面板内 spinner（「首次进入才 spinner」是 v1 已废弃表述）
    expect(second.result.current.loading).toBe(true);
    await waitFor(() => expect(second.result.current.loading).toBe(false));
    expect(fetchTypesMock).toHaveBeenCalledTimes(2);
    expect(fetchPipelinesMock).toHaveBeenCalledTimes(2);
  });
});
