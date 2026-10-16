// Copyright 2026 LexMask Contributors

// Issue #48：ClampTooltipText 组件级回归测试。
// 钉住 Issue#25 A2 的不变量——Tooltip 包裹与根节点（span/p/code）**无条件**渲染，
// 截断状态翻转时根 DOM 节点不得被重建（否则键盘焦点静默丢失）。
// 注意：全站 vitest 默认 node 环境，组件测试逐文件切 jsdom（见本文件头部指令）。
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 注：`IS_REACT_ACT_ENVIRONMENT` 无需手工置位 —— @testing-library/react 自身会在
// 每次 act() 外围 setReactActEnvironment(true)（第二轮机审 M-8：此前的 vi.hoisted
// 块经变异证实是死代码，删掉后 4/4 仍绿、0 act 告警）。

import { ClampTooltipText } from './entity-type-list';

/** jsdom 无 ResizeObserver；hook 有 undefined 兜底，这里给哑实现以覆盖 observer 分支。 */
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

/**
 * jsdom 不做布局，scroll/client 尺寸恒为 0（= 未截断）。截断态用实例属性打桩模拟：
 * Element.prototype 上的这两个 getter 可被实例属性遮蔽，observer/effect 读到的即桩值
 * （另两个维度 scrollWidth/clientWidth jsdom 天然 0/0，无需打桩）。
 */
function setGeometry(
  el: Element,
  { scrollHeight, clientHeight }: { scrollHeight: number; clientHeight: number },
) {
  Object.defineProperty(el, 'scrollHeight', { value: scrollHeight, configurable: true });
  Object.defineProperty(el, 'clientHeight', { value: clientHeight, configurable: true });
}

// 三种 kind 各自必须保持的根节点标签（Issue#25 A2 语义）
const KINDS = [
  { kind: 'title', tag: 'SPAN' },
  { kind: 'description', tag: 'P' },
  { kind: 'regex', tag: 'CODE' },
] as const;

const SHORT = '短文案';
const LONG = '一'.repeat(200); // 长度远超 line-clamp，翻转后必须判为截断

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe('ClampTooltipText · Issue#48 组件级回归', () => {
  for (const { kind, tag } of KINDS) {
    it(`${kind}：未截断 → 截断翻转时根节点不重建（标签恒定 + isSameNode）`, () => {
      const { container, rerender } = render(<ClampTooltipText text={SHORT} kind={kind} />);
      const root = container.querySelector(tag.toLowerCase());
      expect(root).not.toBeNull();
      // 无条件 Tooltip 包裹不引入包装 DOM：根节点直接挂在容器下
      expect(root!.parentElement).toBe(container);
      // 包裹机制**真的还在**：Radix trigger 会把 data-state 合并到子元素上。
      // 若 TooltipProvider/Tooltip/TooltipTrigger 被整体删掉（冻结评审 X8b 变异），
      // 根节点就没有该属性 —— 这条断言专门杀「包裹消失」而 parentElement 看不出来。
      // 注意：data-state 是 Radix 内部属性（现 1.2.8），升级 Radix 时须复核本断言。
      expect(root!.getAttribute('data-state')).toBe('closed');
      // 未截断：不挂 tabIndex（不可聚焦，与原实现一致）
      expect(root!.hasAttribute('tabindex')).toBe(false);

      // 同一节点翻转成截断态：文案变长（key 变化）使 hook 复测，几何量由桩提供
      setGeometry(root!, { scrollHeight: 100, clientHeight: 20 });
      rerender(<ClampTooltipText text={LONG} kind={kind} />);

      const after = container.querySelector(tag.toLowerCase());
      // 同一 DOM 节点（`toBe` 即 `isSameNode` 的判别力来源），根 DOM 未重建、焦点不丢
      expect(after).toBe(root);
      expect(root!.tagName).toBe(tag); // 根节点类型未切换
      expect(root!.parentElement).toBe(container); // 层级未变
      expect(root!.textContent).toBe(LONG); // 文案已更新
      expect(root!.getAttribute('tabindex')).toBe('0'); // 截断：可键盘聚焦

      // 再翻转回未截断：节点仍不重建，tabIndex 复原
      setGeometry(root!, { scrollHeight: 20, clientHeight: 20 });
      rerender(<ClampTooltipText text={SHORT} kind={kind} />);
      expect(root!.isSameNode(container.querySelector(tag.toLowerCase()))).toBe(true);
      expect(root!.tagName).toBe(tag);
      expect(root!.hasAttribute('tabindex')).toBe(false);
    });
  }

  it('截断态：气泡真的还在，且翻转期间键盘焦点不丢（Issue#48 原症状）', () => {
    vi.useFakeTimers();
    try {
      const { container, rerender } = render(<ClampTooltipText text={SHORT} kind="title" />);
      const root = container.querySelector('span')!;

      setGeometry(root, { scrollHeight: 100, clientHeight: 20 });
      rerender(<ClampTooltipText text={LONG} kind="title" />);
      expect(root.getAttribute('tabindex')).toBe('0');

      // B7：截断态下聚焦根节点 → 再换更长的文案，焦点必须仍在**同一节点**上
      act(() => {
        root.focus();
      });
      expect(document.activeElement).toBe(root);
      rerender(<ClampTooltipText text={`${LONG}更长的补充文案`} kind="title" />);
      expect(document.activeElement?.isSameNode(root)).toBe(true);

      // B6：气泡内容真的挂上来了，且展示的是**当前全文**（不是任意占位文字）。
      // 删掉 TooltipContent（冷审 X8a 变异）或把内容换成别的字（二轮机审 X8c/
      // M-7：原先 `toContain('一')` 近乎恒真）时，本断言都必须失败。
      // Radix 的 delayDuration=200ms 用假定时器推进，避免测试下的异步状态更新告警。
      act(() => {
        fireEvent.focus(root);
        vi.advanceTimersByTime(400);
      });
      expect(screen.getByRole('tooltip').textContent).toBe(`${LONG}更长的补充文案`);
    } finally {
      vi.useRealTimers();
    }
  });
});
