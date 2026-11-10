/**
 * Issue#257：失败页清单在批量行上的比较与格式化。
 *
 * 轮询/合并时行对象会被频繁重建，清单必须按值比较，否则每轮都判定「有变化」
 * 导致行组件重渲染（149 页 × 页格矩阵时尤其明显）。
 */

/** 两个页号数组是否逐项相等（顺序敏感：后端已按升序去重返回）。 */
export function sameNumberList(a: readonly number[], b: readonly number[]): boolean {
  if (a.length !== b.length) return false;
  for (let i = 0; i < a.length; i += 1) {
    if (a[i] !== b[i]) return false;
  }
  return true;
}

