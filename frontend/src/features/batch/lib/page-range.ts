// Copyright 2026 LexMask Contributors

// Issue#257 需求重对齐（2026-11-11）：页级选择的三个纯函数。
//
// 用户口径是「200 页的卷要导 100-150 页，不可能手点 51 次」，所以页格必须配
// 一个范围输入（`100-150`、`1-20, 88`、全角标点也认），并支持 Shift 连选。
// 纯函数放这里，页格组件只负责渲染与事件——判定逻辑可以单测，不需要挂 DOM。

/** 单页上限保护：避免 `1-999999` 这种输入把选择集撑爆。 */
const MAX_RANGE_SPAN = 100_000;

/**
 * 解析范围输入，返回升序去重的页号数组。
 *
 * 认这些写法：`100-150`、`100~150`、`100到150`、`1-20, 88`、全角逗号/空格混排。
 * 越界页号被夹到 `[1, maxPages]`，无法解析的片段直接丢弃（不抛错、不打断用户输入）。
 */
export function parsePageRangeInput(text: string, maxPages: number): number[] {
  const limit = Number.isFinite(maxPages) ? Math.max(0, Math.trunc(maxPages)) : 0;
  if (!text || limit <= 0) return [];
  const out = new Set<number>();
  const parts = text.split(/[,，;；\s]+/).filter(Boolean);
  for (const raw of parts) {
    const span = raw.match(/^(\d+)\s*(?:[-~～－—]|到|至)\s*(\d+)$/);
    if (span) {
      let start = Number(span[1]);
      let end = Number(span[2]);
      if (!Number.isFinite(start) || !Number.isFinite(end)) continue;
      if (start > end) [start, end] = [end, start];
      if (end - start > MAX_RANGE_SPAN) end = start + MAX_RANGE_SPAN;
      for (let page = start; page <= end; page += 1) {
        if (page >= 1 && page <= limit) out.add(page);
      }
      continue;
    }
    if (/^\d+$/.test(raw)) {
      const page = Number(raw);
      if (page >= 1 && page <= limit) out.add(page);
    }
  }
  return [...out].sort((a, b) => a - b);
}

/** Shift 连选：把 `[from, to]` 之间所有可选页并入选择集（反向也成立）。 */
export function extendSelection(
  selected: ReadonlySet<number>,
  from: number,
  to: number,
  isSelectable: (page: number) => boolean,
): Set<number> {
  const next = new Set(selected);
  const start = Math.min(from, to);
  const end = Math.max(from, to);
  for (let page = start; page <= end; page += 1) {
    if (isSelectable(page)) next.add(page);
  }
  return next;
}

/**
 * 剪枝：识别进行中时某页可能从 success 变成 failed，选择集必须跟着失效
 * （否则用户会以为选了、导出里却没有）。没有任何变化时返回原实例，避免多余渲染。
 */
export function pruneSelection(
  selected: ReadonlySet<number>,
  isSelectable: (page: number) => boolean,
): Set<number> {
  let changed = false;
  const next = new Set<number>();
  selected.forEach((page) => {
    if (isSelectable(page)) next.add(page);
    else changed = true;
  });
  return changed ? next : (selected as Set<number>);
}
