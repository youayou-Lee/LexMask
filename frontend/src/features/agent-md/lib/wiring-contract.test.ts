// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

// Issue#212 门⑥ I4（F1）：页面 → API 的 options 转发此前零判据——把
// `agentMdApi.confirm(taskId, decisions)` 写成丢 options 的形态，vitest 仍全绿，
// 而后果是"用户勾了知情框仍永远 409、无法交付"。
//
// 表达式级契约：只钉接线本身，不替代 `api.test.ts`（网络层载荷）与
// `step-review.zero-entities.test.tsx`（组件行为）。
const sources = import.meta.glob('../agent-md-page.tsx', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

const page = sources['../agent-md-page.tsx'];
if (typeof page !== 'string') {
  throw new Error(`wiring-contract: 读不到 agent-md-page.tsx（可用键：${Object.keys(sources).join(', ')}）`);
}

describe('供料模式接线（Issue#212）', () => {
  it('页面 confirm 必须把 options 转发给 API', () => {
    expect(page).toMatch(/agentMdApi\.confirm\(\s*taskId\s*,\s*decisions\s*,\s*options\s*\)/);
  });
});
