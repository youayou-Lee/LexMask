// Copyright 2026 LexMask Contributors

// Issue#351 P2-E（v2 方案）：术语残留清零。
// E1：batchWizard.status.awaitingReview 待审阅→待复核（en 已是 Awaiting review，钉不变）。
// E2：singleTask.phase.awaiting_review（待确认）死键删除——缺席断言
//     （冷审 C1：tsc/parity 对删键零判别力，必须 not.toHaveProperty）。

import { describe, expect, it } from 'vitest';

import { zh } from './zh';
import { en } from './en';

describe('Issue#351 P2-E：术语残留清零', () => {
  it('E1：向导 awaitingReview 状态词为「待复核」（zh 值钉；en 钉不变）', () => {
    expect(zh['batchWizard.status.awaitingReview']).toBe('待复核');
    expect(en['batchWizard.status.awaitingReview']).toBe('Awaiting review');
  });

  it('D2-en：供料副标题 en 短句同步（冷审 I6/门⑥ Minor-3 双钉）', () => {
    expect(en['nav.agentMd.sub']).toBe('Case files to sanitized Markdown');
  });

  it('E2：死键 singleTask.phase.awaiting_review 已从 zh/en 删除（缺席断言）', () => {
    expect(zh).not.toHaveProperty('singleTask.phase.awaiting_review');
    expect(en).not.toHaveProperty('singleTask.phase.awaiting_review');
    // 正向对照：活键仍在（防整个 phase 组被误删）
    expect(zh['singleTask.phase.recognizing']).toBe('识别中');
    expect(zh['singleTask.phase.redacting']).toBe('打码中');
  });
});