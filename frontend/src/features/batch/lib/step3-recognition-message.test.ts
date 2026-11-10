// Copyright 2026 LexMask Contributors

// Issue#256：step3 识别消息映射契约钉——含「视觉段跳过」终态；未知消息必须
// 返回 null（回落显示原文），不得误映射。
import { describe, expect, it } from 'vitest';

import { batchRecognitionMessageKey } from './step3-recognition-message';

describe('batchRecognitionMessageKey', () => {
  it('maps the vision-skipped terminal code introduced by Issue#256', () => {
    expect(batchRecognitionMessageKey('vision_recognition_complete_visual_skipped')).toBe(
      'batchWizard.step3.fileVisionCompleteSkipped',
    );
  });

  it('maps the pre-existing terminal codes unchanged', () => {
    expect(batchRecognitionMessageKey('vision_recognition_complete')).toBe(
      'batchWizard.step3.fileVisionComplete',
    );
    expect(batchRecognitionMessageKey('text_recognition_complete')).toBe(
      'batchWizard.step3.fileTextComplete',
    );
    expect(batchRecognitionMessageKey('text_recognition_running')).toBe(
      'batchWizard.step3.fileTextProgress',
    );
    expect(batchRecognitionMessageKey('Requeued after stale processing heartbeat')).toBe(
      'batchWizard.step3.requeuedAfterStale',
    );
  });

  it('returns null for unknown/empty messages so the UI falls back to raw text', () => {
    expect(batchRecognitionMessageKey('some_future_code')).toBeNull();
    expect(batchRecognitionMessageKey('')).toBeNull();
    expect(batchRecognitionMessageKey(null)).toBeNull();
    expect(batchRecognitionMessageKey(undefined)).toBeNull();
  });
});
