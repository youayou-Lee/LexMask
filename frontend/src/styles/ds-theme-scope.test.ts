// Copyright 2026 LexMask Contributors

// Issue#355 作用域契约（验收方案 v2 ①4a）：
// ds-theme.css 的每一条选择器都必须以 [data-theme="ds"] 为前缀——
// classic（升级前存量视觉）= 根节点无该属性 ⇒ 本文件必须完全不生效。
// 任何不带前缀的选择器都是对 classic 的样式泄漏（回归面）。
//
// 读取方式：vitest 默认 stub CSS 模块（实测连 `?raw` 也返回空串），因此用
// node:fs 直读源文件。@types/node 未引入（Issue#157 教训：装它会外溢全局
// NodeJS 类型面，改产品代码的 setTimeout 推断），用 @ts-expect-error 压
// import 的 TS2307——若未来引入 @types/node，这条会因「无错可压」编译失败，
// 到时删掉本行即可（fail-loud，不静默漂移）。
//
// 防空转双保险：
// - 断言文件含已知样例规则且体量非平凡——把文件删空/清空时本测试必须红；
// - 注释先剥除再校验，注释里出现 "[data-theme" 不算数。

// @ts-expect-error node:fs 暂无类型声明（@types/node 刻意未装，见上）
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

const css = readFileSync(new URL('./ds-theme.css', import.meta.url), 'utf-8');

const SCOPE = '[data-theme="ds"]';

/** 找出所有不带作用域前缀的选择器行（注释先剥除；@keyframes 体与 at-rule 头豁免）。 */
function unscopedSelectors(source: string): string[] {
  const body = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const violations: string[] = [];
  let depth = 0;
  // @keyframes 体（from/to/百分比选择器）不受作用域约束：记住进入前深度，
  // 深度回落到该值即视为 keyframes 块闭合。
  let keyframesEntryDepth: number | null = null;
  for (const raw of body.split('\n')) {
    const line = raw.trim();
    if (!line) continue;
    const opens = (line.match(/\{/g) ?? []).length;
    const closes = (line.match(/\}/g) ?? []).length;

    if (line.startsWith('@keyframes')) {
      keyframesEntryDepth = depth;
      depth += opens - closes;
      continue;
    }
    if (keyframesEntryDepth !== null) {
      depth += opens - closes;
      if (depth === keyframesEntryDepth) keyframesEntryDepth = null;
      continue;
    }

    // 选择器行（含 "{"）必须是 at-rule 头或带作用域前缀；纯声明/闭块行跳过。
    if (
      line.includes('{') &&
      !line.startsWith(SCOPE) &&
      !/^@(media|supports|layer|container|property)\b/.test(line)
    ) {
      violations.push(line);
    }
    depth += opens - closes;
  }
  return violations;
}

describe('Issue#355 ds-theme.css 作用域契约（classic 零泄漏）', () => {
  it('每条选择器都带 [data-theme="ds"] 前缀', () => {
    expect(unscopedSelectors(css)).toEqual([]);
  });

  it('文件非空且含已知样例规则（删空文件时本测试必须红）', () => {
    expect(css).toContain(`${SCOPE} {`);
    expect(css).toContain(`${SCOPE} .jobs-table-list > li > div`);
    expect(css.length).toBeGreaterThan(2000);
  });

  // Issue#355 Agent A（2026-11-19 iOS 重置）壳与组件段的接线钉：
  // 玻璃顶栏 / 侧栏 active 绿胶囊 / 分段控件 / 主按钮胶囊 / iOS 开关在位且带作用域。
  it('壳与基础组件段关键规则在位（删段时本测试必须红）', () => {
    expect(css).toContain(`${SCOPE} .app-shell-header {`);
    expect(css).toContain(`${SCOPE} .ds-sidebar-shell {`);
    expect(css).toContain(`${SCOPE} [data-sidebar="sidebar"] {`);
    expect(css).toContain(`${SCOPE} [data-sidebar="menu-button"][data-active="true"] {`);
    expect(css).toContain(`${SCOPE} .ds-seg {`);
    expect(css).toContain(`${SCOPE} .ds-btn-primary {`);
    expect(css).toContain(`${SCOPE} .ds-switch[data-state='checked'] {`);
    expect(css).toContain(`${SCOPE} .ds-pill {`);
    expect(css).toContain(`${SCOPE} .ds-page-title {`);
    expect(css).toContain(`${SCOPE} .ds-page-cta {`);
  });
});
