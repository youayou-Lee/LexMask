// Copyright 2026 LexMask Contributors

// Issue#74：还原请求构造。policy 只认 safe/first 两档（后端对非法值 400），
// 归一兜底 safe——与后端默认档一致，脏值不外发。

export type RestorePolicy = 'safe' | 'first';

export function normalizePolicy(value: unknown): RestorePolicy {
  return value === 'first' ? 'first' : 'safe';
}

export interface RestoreRequestBody {
  text: string;
  mapping: Record<string, unknown>;
  policy: RestorePolicy;
}

export function buildRestoreRequest(
  text: string,
  mapping: Record<string, unknown>,
  policy: unknown,
): RestoreRequestBody {
  return { text, mapping, policy: normalizePolicy(policy) };
}
