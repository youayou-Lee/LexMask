// Copyright 2026 LexMask Contributors

// Issue#196 供料模式 step1 图像总览卡门控（本地浏览器验收发现的接线缺口）：
// BatchStep1PresetCards 对 agent 隐藏了视觉配置卡，但 BatchStep1PreviewCards 的
// showImagePreview 仍是 mode !== 'text'——供料向导 step1 仍显示「图像识别项 /
// 图片打码：马赛克」总览卡，与「只选文本识别项、隐藏图像打码配置」相悖。
// 判据钉在 data-testid 上（删除门控行即红），既有 smart/text 模式不回归。
//
// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BatchStep1PreviewCards } from './batch-step1-preview';

afterEach(() => cleanup());

const BASE = {
  textPreviewGroups: [{ title: '固定格式文字', items: ['日期'], emptyLabel: '无' }],
  imagePreviewGroups: [{ title: '图片中的文字', items: ['姓名'], emptyLabel: '无' }],
  textPresetName: '推荐混合清单',
  visionPresetName: '推荐混合清单',
  textModeLabel: '清晰标签',
  imageDetailPills: ['图片打码：马赛克', '强度：75%'],
  previewDialog: null,
  setPreviewDialog: vi.fn(),
};

function renderCards(mode: 'text' | 'image' | 'smart' | 'agent') {
  return render(<BatchStep1PreviewCards {...BASE} mode={mode} />);
}

describe('供料向导 step1 图像总览卡门控（Issue#196）', () => {
  it('agent 模式：图像总览卡不渲染，文本总览卡在位', () => {
    renderCards('agent');
    expect(screen.queryByTestId('step1-image-summary-card')).toBeNull();
    expect(screen.getByText('文本识别项')).toBeTruthy();
  });

  it('smart 模式：两卡照常（既有模式不回归）', () => {
    renderCards('smart');
    expect(screen.getByTestId('step1-image-summary-card')).toBeTruthy();
    expect(screen.getByText('文本识别项')).toBeTruthy();
  });

  it('text 模式：只显文本卡（既有门控不回归）', () => {
    renderCards('text');
    expect(screen.queryByTestId('step1-image-summary-card')).toBeNull();
    expect(screen.getByText('文本识别项')).toBeTruthy();
  });
});
