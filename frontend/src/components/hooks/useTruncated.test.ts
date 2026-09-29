// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

import { isElementTruncated } from './useTruncated';

const el = (scroll: { height: number; width: number }, client: { height: number; width: number }) => ({
  scrollHeight: scroll.height,
  scrollWidth: scroll.width,
  clientHeight: client.height,
  clientWidth: client.width,
});

describe('isElementTruncated', () => {
  it('detects vertical clipping (line-clamp)', () => {
    expect(isElementTruncated(el({ height: 64, width: 200 }, { height: 48, width: 200 }))).toBe(true);
  });

  it('tolerates 1px rounding noise from fractional DPI scaling', () => {
    expect(isElementTruncated(el({ height: 49, width: 200 }, { height: 48, width: 200 }))).toBe(false);
    expect(isElementTruncated(el({ height: 200, width: 201 }, { height: 200, width: 200 }))).toBe(false);
    expect(isElementTruncated(el({ height: 50, width: 200 }, { height: 48, width: 200 }))).toBe(true);
  });

  it('detects horizontal clipping', () => {
    expect(isElementTruncated(el({ height: 16, width: 500 }, { height: 16, width: 200 }))).toBe(true);
  });

  it('returns false when content fits exactly', () => {
    expect(isElementTruncated(el({ height: 48, width: 200 }, { height: 48, width: 200 }))).toBe(false);
  });

  it('returns false when content is smaller than the box', () => {
    expect(isElementTruncated(el({ height: 16, width: 100 }, { height: 48, width: 200 }))).toBe(false);
  });
});
