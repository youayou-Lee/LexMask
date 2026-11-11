// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

import { sanitizeNextPath } from './auth-routing';

describe('Issue#262：登录默认落点改 /single', () => {
  it('无 next 时默认 /single 而非 /', () => {
    expect(sanitizeNextPath(undefined)).toBe('/single');
  });
  it('显式 next 仍然尊重', () => {
    expect(sanitizeNextPath('/history?source=batch')).toBe('/history?source=batch');
  });
});
