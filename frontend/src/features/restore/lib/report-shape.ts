// Copyright 2026 LexMask Contributors

// Issue#74：还原报告整形。HTTP 响应的 ambiguous 只有 {key, candidates,
// reason?}（service 层的 occurrences 被 API 模型丢弃，冷审实查）——次数列
// 按方案 v2 由前端自算：该 key 在输入文本中的出现次数（safe 档占位符保留
// 在文中，语义即「这个占位符还有几处待仲裁」）。

export interface RestoreAmbiguousItem {
  key: string;
  candidates: string[];
  reason?: string | null;
}

export interface RestoreResponse {
  restored_text: string;
  restored_count: number;
  ambiguous: RestoreAmbiguousItem[];
  unknown: string[];
  hits: Record<string, unknown>;
  parse_warnings: string[];
}

export interface AmbiguousRow {
  key: string;
  candidates: string[];
  occurrences: number;
  reason: string | null;
}

export function countOccurrences(text: string, key: string): number {
  if (!key) return 0;
  return text.split(key).length - 1;
}

export function buildAmbiguousRows(
  inputText: string,
  ambiguous: RestoreAmbiguousItem[],
): AmbiguousRow[] {
  return ambiguous.map((item) => ({
    key: item.key,
    candidates: item.candidates,
    occurrences: countOccurrences(inputText, item.key),
    reason: item.reason ?? null,
  }));
}

const UNKNOWN_MAX_SHOW = 8;

export function formatUnknownList(
  unknown: string[],
  maxShow: number = UNKNOWN_MAX_SHOW,
): { shown: string[]; overflow: number } {
  return {
    shown: unknown.slice(0, maxShow),
    overflow: Math.max(unknown.length - maxShow, 0),
  };
}
