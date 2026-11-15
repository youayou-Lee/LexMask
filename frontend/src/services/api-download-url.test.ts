// Copyright 2026 LexMask Contributors
import { describe, expect, it } from 'vitest';
import { fileApi } from './api';

// 注意：本文件只钉 URL 构造器本身（纯函数）。「生产侧真的走它」由
// features/playground/wiring-contract.test.ts 钉住（上一轮这里断言的是自己传的 URL，
// 删掉生产代码也不红 ⇒ 假保证，门⑥ 三轮必修已修）。
describe('fileApi.getDownloadUrl URL 构造器（Issue#325 preview）', () => {
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
