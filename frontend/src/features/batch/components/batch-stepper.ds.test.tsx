// @vitest-environment jsdom
// Copyright 2026 LexMask Contributors

// Issue#355 Agent C：批量向导胶囊步骤条 ds 状态钩子钉（真稿 ios-batch-wizard .stepper：
// 完成绿勾 / 当前墨圈 / 未到灰，连接线完成段绿色）。
// 钩子形态：按钮 data-state='done|current|idle|locked' + 号码圆片 .ds-stepper-n +
// 连接线 .ds-stepper-line[data-done]。classic 无 CSS 匹配零影响（DOM 属性无害）。

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { useI18n } from '@/i18n';
import { BatchStepProgress } from './batch-step-progress';
import type { Step } from '../types';

function makeProps(currentStep: Step) {
  return {
    currentStep,
    // 只有 ≤ 当前步可回访（真实向导语义的近似：未到步不可跳）
    canGoStep: (s: Step) => s <= currentStep,
    goStep: vi.fn(),
  };
}

function stepperDom() {
  const nav = screen.getByTestId('batch-step-progress');
  const buttons = Array.from(nav.querySelectorAll<HTMLButtonElement>('[data-testid^="batch-step-"]'));
  const lines = Array.from(nav.querySelectorAll<HTMLElement>('.ds-stepper-line'));
  return { nav, buttons, lines };
}

afterEach(() => {
  cleanup();
  useI18n.setState({ locale: 'zh' });
});

describe('Issue#355 批量向导步骤条 ds 状态钩子', () => {
  it('currentStep=2：步1=done、步2=current(aria-current)、步3-5=locked；连接线 1=done', () => {
    useI18n.setState({ locale: 'zh' });
    render(<BatchStepProgress {...makeProps(2)} />);
    const { buttons, lines } = stepperDom();

    expect(buttons).toHaveLength(5);
    expect(buttons[0]?.getAttribute('data-state')).toBe('done');
    expect(buttons[0]?.querySelector('.ds-stepper-n')).not.toBeNull();
    expect(buttons[1]?.getAttribute('data-state')).toBe('current');
    expect(buttons[1]?.getAttribute('aria-current')).toBe('step');
    for (const idx of [2, 3, 4]) {
      expect(buttons[idx]?.getAttribute('data-state')).toBe('locked');
    }
    // 连接线：步1→2 段已完成（绿），其余未到（灰）
    expect(lines).toHaveLength(4);
    expect(lines[0]?.getAttribute('data-done')).toBe('true');
    for (const idx of [1, 2, 3]) {
      expect(lines[idx]?.getAttribute('data-done')).toBe('false');
    }
  });

  it('currentStep=5：步1-4=done、步5=current；全部连接线 done', () => {
    useI18n.setState({ locale: 'zh' });
    render(<BatchStepProgress {...makeProps(5)} />);
    const { buttons, lines } = stepperDom();

    for (const idx of [0, 1, 2, 3]) {
      expect(buttons[idx]?.getAttribute('data-state')).toBe('done');
    }
    expect(buttons[4]?.getAttribute('data-state')).toBe('current');
    for (const line of lines) {
      expect(line.getAttribute('data-done')).toBe('true');
    }
  });

  it('号码圆片 .ds-stepper-n 常驻（5 步各一），文案不变（classic aria 契约不受影响）', () => {
    useI18n.setState({ locale: 'zh' });
    render(<BatchStepProgress {...makeProps(1)} />);
    const { nav } = stepperDom();
    expect(nav.querySelectorAll('.ds-stepper-n')).toHaveLength(5);
    // 既有契约：aria-label=步骤总览（batch-hub-nav C1 钉）
    expect(
      screen.getByLabelText('五步：选择清单 → 上传 → 批量识别 → 审阅确认 → 导出'),
    ).toBeTruthy();
  });
});
