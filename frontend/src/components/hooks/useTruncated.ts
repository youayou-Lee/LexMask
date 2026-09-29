// Copyright 2026 LexMask Contributors

import { useEffect, useState } from 'react';

/** Issue #9：任一维度 scroll 尺寸超过 client 尺寸即视为内容被截断（如 line-clamp）。 */
export function isElementTruncated(el: {
  scrollHeight: number;
  clientHeight: number;
  scrollWidth: number;
  clientWidth: number;
}): boolean {
  return el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth;
}

/**
 * 检测元素内容是否被 CSS 截断，用于「仅截断时」挂 Tooltip 显示全文。
 * 回调 ref 挂目标元素；自身尺寸变化（ResizeObserver）或 `key` 变化（文案替换）时复测。
 */
export function useTruncated(key: string) {
  const [node, setNode] = useState<HTMLElement | null>(null);
  const [truncated, setTruncated] = useState(false);

  useEffect(() => {
    if (!node) return;
    const update = () => setTruncated(isElementTruncated(node));
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(node);
    return () => observer.disconnect();
  }, [node, key]);

  return { ref: setNode, truncated };
}
