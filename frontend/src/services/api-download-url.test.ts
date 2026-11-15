// Copyright 2026 LexMask Contributors
import { describe, expect, it } from 'vitest';
import { fileApi } from './api';

describe('fileApi.getDownloadUrl (Issue#325 preview)', () => {
  it('defaults to full redacted artifact', () => {
    expect(fileApi.getDownloadUrl('f1', true)).toContain('/files/f1/download?redacted=true');
    expect(fileApi.getDownloadUrl('f1', true)).not.toContain('preview');
  });
  it('reads subset preview artifact with preview=true', () => {
    expect(fileApi.getDownloadUrl('f1', true, true)).toBe(
      '/api/v1/files/f1/download?redacted=true&preview=true',
    );
  });
});
