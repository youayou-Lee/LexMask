// Copyright 2026 LexMask Contributors

// Issue#355 作用域契约 · Agent B 分册：ios-pages-b.css 的每一条选择器
// 都必须以 [data-theme="ds"] 为前缀（机制与 ds-theme-scope.test.ts 相同——
// classic = 根节点无该属性 ⇒ 本文件完全不生效）。
// 读取方式用 node:fs 直读源文件（vitest 默认 stub CSS 模块，?raw 也返回空串）。

// @ts-expect-error node:fs 暂无类型声明（@types/node 刻意未装）
import { readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

const css = readFileSync(new URL('./ios-pages-b.css', import.meta.url), 'utf-8');

const SCOPE = '[data-theme="ds"]';

function unscopedSelectors(source: string): string[] {
  const body = source.replace(/\/\*[\s\S]*?\*\//g, '');
  const violations: string[] = [];
  let depth = 0;
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

describe('Issue#355 ios-pages-b.css 作用域契约', () => {
  it('文件含已知样例规则且体量非平凡（防空转）', () => {
    expect(css).toContain('.ds-metric-chip');
    expect(css).toContain('.ios-pager');
    expect(css.replace(/\/\*[\s\S]*?\*\//g, '').length).toBeGreaterThan(2000);
  });

  it('每一条选择器都带 [data-theme="ds"] 前缀', () => {
    expect(unscopedSelectors(css)).toEqual([]);
  });
});
