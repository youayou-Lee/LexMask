// Copyright 2026 LexMask Contributors

// Issue#194 Bug#1 钉：Layout 的 Provider 嵌套顺序。
//
// 不变量：SingleTaskProvider 必须是 PlaygroundProvider 的祖先。
// PlaygroundProvider 自身渲染即调用 usePlayground()/usePlaygroundFile()，
// 二者消费 useSingleTask()（识别段任务账本，T9 接线）——React Context 只向
// 下游可见，反序（PlaygroundProvider 在外）会让这些 hook 在 SingleTaskProvider
// 上方运行 ⇒ 「useSingleTask must be used within <SingleTaskProvider>」全路由白屏。
//
// 取舍说明：渲染真实 <Layout/> 需要连根 mock playground 整条链（auth/health/
// presets/api-client/路由/侧边栏 UI），重且脆；这里退而求其次钉源码文本——
// 断言两层 Provider 的开/闭标签次序 + AppSidebar 位置。这是文本级判据（非
// 渲染级）：若将来改用自闭合/组合式写法需同步本钉，换来的是对回退（谁先谁后）
// 的零成本直读。源码读取走 Vite `?raw` 导入（同 wiring-contract.test.ts，
// Issue#157：不引 @types/node，不改产品类型面）。

import layoutSource from './index.tsx?raw';

import { describe, expect, it } from 'vitest';

function indexOfOrThrow(needle: string): number {
  const at = layoutSource.indexOf(needle);
  expect(at, `Layout/index.tsx 应包含 ${needle}（判据与实现同步维护）`).toBeGreaterThanOrEqual(0);
  return at;
}

describe('Issue#194 Bug#1：Layout Provider 嵌套顺序（SingleTaskProvider 外层）', () => {
  it('SingleTaskProvider 是 PlaygroundProvider 的祖先（开标签在前、闭标签在后）', () => {
    const singleOpen = indexOfOrThrow('<SingleTaskProvider>');
    const playgroundOpen = indexOfOrThrow('<PlaygroundProvider>');
    const playgroundClose = indexOfOrThrow('</PlaygroundProvider>');
    const singleClose = indexOfOrThrow('</SingleTaskProvider>');

    expect(
      playgroundOpen > singleOpen,
      'SingleTaskProvider 开标签必须在 PlaygroundProvider 之前（playground hooks 消费 useSingleTask，Context 只向下游可见）',
    ).toBe(true);
    expect(
      playgroundClose < singleClose,
      'PlaygroundProvider 闭标签必须在 SingleTaskProvider 闭标签之前（真嵌套，而非仅文本相邻）',
    ).toBe(true);
  });

  it('AppSidebar 是两层 Provider 的共同后代（侧边栏角标消费 useSingleTask）', () => {
    const playgroundOpen = indexOfOrThrow('<PlaygroundProvider>');
    const playgroundClose = indexOfOrThrow('</PlaygroundProvider>');
    const appSidebar = indexOfOrThrow('<AppSidebar');

    expect(appSidebar > playgroundOpen, 'AppSidebar 必须在 PlaygroundProvider 之内').toBe(true);
    expect(appSidebar < playgroundClose, 'AppSidebar 必须在 PlaygroundProvider 之内').toBe(true);
  });
});
