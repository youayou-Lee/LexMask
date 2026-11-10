// Copyright 2026 LexMask Contributors

import { getEntityTypeName } from '@/config/entityTypes';
import { localizeRecognitionTypeName } from '@/features/settings/lib/redaction-display';

/**
 * Issue#262 二期 Task 10b 收敛：原 TextTypeGroups / VisionPipelines / ConfigEmptyState
 * 等抽屉内渲染组件已随 T6 内联抽屉移除（识别范围改三区弹窗，见 recognition-scope-dialog.tsx，
 * chip 渲染与分页在弹窗内自实现）。本文件只保留两处共用纯函数。
 */

// Drop repeated chips: same id, or same display name (e.g. a type surfaced twice
// across domains). Keeps the first occurrence so the playground never shows a label twice.
export function dedupeRecognitionTypes<T extends { id?: string; name?: string }>(
  types: readonly T[],
): T[] {
  const seenId = new Set<string>();
  const seenName = new Set<string>();
  const out: T[] = [];
  for (const ty of types) {
    const id = String(ty.id ?? '');
    const name = String(ty.name ?? '');
    if ((id && seenId.has(id)) || (name && seenName.has(name))) continue;
    if (id) seenId.add(id);
    if (name) seenName.add(name);
    out.push(ty);
  }
  return out;
}

export function resolveTextTypeName(
  typeId: string,
  fallbackName: string | undefined,
  t: (key: string) => string,
) {
  return localizeRecognitionTypeName({ id: typeId, name: fallbackName }, t) || getEntityTypeName(typeId);
}
