// Copyright 2026 LexMask Contributors

import type { ReactNode } from 'react';
import { getEntityTypeName } from '@/config/entityTypes';
import { localizeRecognitionTypeName } from '@/features/settings/lib/redaction-display';

/**
 * Issue#262 二期 Task 10b 收敛：原 TextTypeGroups / VisionPipelines 等抽屉内渲染组件
 * 已随 T6 内联抽屉移除（识别范围改三区弹窗，见 recognition-scope-dialog.tsx，
 * chip 渲染与分页在弹窗内自实现）。
 *
 * Issue#267：ConfigEmptyState 在此原址恢复（初始提交 55002ec 定义、c71035a 随 T6 移除），
 * 视觉等价复用（虚线框+居中标题+描述）；testId 参数化供弹窗三态面板
 * （-loading/-empty/-unavailable）各自命名，旧默认值保留向后兼容。
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

export function ConfigEmptyState({
  title,
  description,
  testId = 'playground-config-empty',
  children,
}: {
  title: string;
  description?: string;
  testId?: string;
  children?: ReactNode;
}) {
  return (
    <div
      className="rounded-[20px] border border-dashed border-border/70 bg-muted/20 px-5 py-8 text-center"
      data-testid={testId}
    >
      <p className="text-sm font-semibold tracking-[-0.02em] text-foreground">{title}</p>
      {description && (
        <p className="mt-2 text-xs leading-6 text-muted-foreground">{description}</p>
      )}
      {children}
    </div>
  );
}
