// Copyright 2026 LexMask Contributors

// Issue#74：映射表前端预检——只拦顶层结构错误；条目级非法（texts 空/值类型
// 不支持）必须放行给后端，由响应里的 parse_warnings 展示。预检做狠了会让
// parse_warnings 在 UI 上永远无法触发（冷审分歧点 3 钉死的边界）。

/** 大文本护栏：粘贴/上传超过 2MB 前端拦截（后端 60MB 中间件兜底前的提前挡） */
export const RESTORE_TEXT_MAX_BYTES = 2 * 1024 * 1024;

export type MappingPrecheckFailure = {
  ok: false;
  reason: 'invalidJson' | 'notObject' | 'emptyMapping';
  detail?: string;
};

export type MappingPrecheckSuccess = {
  ok: true;
  mapping: Record<string, unknown>;
};

export type MappingPrecheckResult = MappingPrecheckSuccess | MappingPrecheckFailure;

export function parseMappingJson(raw: string): MappingPrecheckResult {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    return {
      ok: false,
      reason: 'invalidJson',
      detail: error instanceof Error ? error.message : undefined,
    };
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    return { ok: false, reason: 'notObject' };
  }
  const mapping = parsed as Record<string, unknown>;
  // {} 是后端合法输入（占位符全进 unknown），但那必然是无用结果——按 UX 规则
  // 拦下提示「映射表为空」（冷审分歧点 4：自定规则，由测试钉死）。
  if (Object.keys(mapping).length === 0) {
    return { ok: false, reason: 'emptyMapping' };
  }
  return { ok: true, mapping };
}

export function exceedsSizeLimit(text: string, limitBytes: number = RESTORE_TEXT_MAX_BYTES): boolean {
  return new TextEncoder().encode(text).byteLength > limitBytes;
}
