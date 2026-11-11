// Copyright 2026 LexMask Contributors

// Issue#257 需求重对齐（2026-11-11）：范围输入 + Shift 连选的纯函数契约。
import { describe, expect, it } from 'vitest';

import { parsePageRangeInput, extendSelection, pruneSelection } from './page-range';

describe('parsePageRangeInput', () => {
  it('parses a single range', () => {
    expect(parsePageRangeInput('100-150', 200)).toEqual(
      Array.from({ length: 51 }, (_, i) => 100 + i),
    );
  });

  it('parses ranges, singles and mixes with spaces / full-width punctuation', () => {
    expect(parsePageRangeInput('1-3， 8 10～12', 200)).toEqual([1, 2, 3, 8, 10, 11, 12]);
  });

  it('reverses a backwards range and dedups', () => {
    expect(parsePageRangeInput('5-3', 200)).toEqual([3, 4, 5]);
    expect(parsePageRangeInput('1-2,2-3,3', 200)).toEqual([1, 2, 3]);
  });

  it('clamps to the document and drops junk', () => {
    expect(parsePageRangeInput('0-2, 199-999', 200)).toEqual([1, 2, 199, 200]);
    expect(parsePageRangeInput('abc, -5, 3.7', 200)).toEqual([]);
    expect(parsePageRangeInput('', 200)).toEqual([]);
  });
});

describe('extendSelection', () => {
  const selectable = (p: number) => p !== 7;

  it('adds the whole span and skips unselectable pages', () => {
    const next = extendSelection(new Set([1]), 5, 9, selectable);
    expect([...next].sort((a, b) => a - b)).toEqual([1, 5, 6, 8, 9]);
  });

  it('works backwards too', () => {
    const next = extendSelection(new Set(), 9, 8, selectable);
    expect([...next].sort((a, b) => a - b)).toEqual([8, 9]);
  });
});

describe('pruneSelection', () => {
  it('drops pages that stopped being selectable', () => {
    const next = pruneSelection(new Set([1, 2, 3]), (p) => p !== 2);
    expect([...next].sort((a, b) => a - b)).toEqual([1, 3]);
  });

  it('returns the same instance when nothing dropped', () => {
    const sel = new Set([1, 2]);
    expect(pruneSelection(sel, () => true)).toBe(sel);
  });
});
