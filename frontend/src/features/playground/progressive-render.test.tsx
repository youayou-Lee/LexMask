// Copyright 2026 LexMask Contributors

// Issue#123：识别期间渐进渲染的判据与遮罩契约（本地可跑、可失败）。
//
// 钉住两条不变量：
// 1) 多页扫描件/图片在 parse 完成后立即进入 preview（否则画布不在渲染树里，
//    逐页回流的框无处可显 ⇒ 首屏 = 全卷）；
// 2) 仅多页流式识别期间把全屏遮罩降级为 passive（透明/不遮挡/不锁交互），
//    单页与文本路径必须保持 blocking。
//
// 红先依据：本文件所依赖的模块与 `overlay` 属性在基线树（fcac61a）上不存在
// ⇒ 同一用例在基线树上必红（见门② 证据）。
//
// 判据边界（门①.5 r2）：jsdom 无布局，**命中测试（elementFromPoint / 截图）一律归浏览器臂**，
// 本文件只判「模块契约 + 元素存在性 + 类名档位」；「画布已挂载且第 1 页框节点在 document 内」
// 是 AC1(a) 的**承重断言**，钉在浏览器臂（云端）上执行。
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { PlaygroundLoading } from './components/playground-loading';
import {
  overlayModeFor,
  shouldEnterPreviewDuringRecognition,
  shouldUsePassiveOverlay,
} from './lib/progressive-render';

afterEach(() => {
  cleanup();
});

describe('shouldEnterPreviewDuringRecognition（Issue#123 第一层根因）', () => {
  it('多页扫描件 ⇒ 立即进 preview', () => {
    expect(
      shouldEnterPreviewDuringRecognition({ pageCount: 10, isScanned: true, fileType: 'pdf' }),
    ).toBe(true);
  });

  it('多页图片 ⇒ 立即进 preview', () => {
    expect(
      shouldEnterPreviewDuringRecognition({ pageCount: 3, isScanned: false, fileType: 'image' }),
    ).toBe(true);
  });

  it('单页扫描件 ⇒ 保持既有行为（识别结束才切 preview）', () => {
    expect(
      shouldEnterPreviewDuringRecognition({ pageCount: 1, isScanned: true, fileType: 'pdf' }),
    ).toBe(false);
  });

  it('多页文本 PDF（非扫描） ⇒ 不提前进 preview', () => {
    expect(
      shouldEnterPreviewDuringRecognition({ pageCount: 10, isScanned: false, fileType: 'pdf' }),
    ).toBe(false);
  });

  it('页数为 0 ⇒ false（不得因 0 触发提前切段）', () => {
    expect(
      shouldEnterPreviewDuringRecognition({ pageCount: 0, isScanned: true, fileType: 'image' }),
    ).toBe(false);
  });
});

describe('shouldUsePassiveOverlay（遮罩降级判据）', () => {
  it('加载中 + 多页流式 ⇒ passive', () => {
    expect(shouldUsePassiveOverlay(true, { completed: 1, total: 10 })).toBe(true);
  });

  it('加载中 + 单页 ⇒ 仍 blocking（不得因首页回流就提前解遮罩）', () => {
    expect(shouldUsePassiveOverlay(true, { completed: 0, total: 1 })).toBe(false);
  });

  it('加载中 + 无流式进度（文本 NER / 非流式） ⇒ blocking', () => {
    expect(shouldUsePassiveOverlay(true, null)).toBe(false);
  });

  it('非加载态 ⇒ blocking（遮罩本就不渲染，判据不得为真）', () => {
    expect(shouldUsePassiveOverlay(false, { completed: 5, total: 10 })).toBe(false);
  });
});

describe('overlayModeFor（表现层接线档位）', () => {
  it('渐进渲染中 ⇒ passive', () => {
    expect(overlayModeFor(true)).toBe('passive');
  });

  it('非渐进渲染 ⇒ blocking', () => {
    expect(overlayModeFor(false)).toBe('blocking');
  });
});

describe('PlaygroundLoading 遮罩契约', () => {
  it('默认（未传 overlay）＝ blocking：全屏半透明 + 模糊', () => {
    render(<PlaygroundLoading loadingMessage="识别中" isImageMode={false} />);
    const root = screen.getByTestId('playground-loading');
    expect(root.getAttribute('data-overlay')).toBe('blocking');
    expect(root.className).toContain('backdrop-blur-[2px]');
    expect(root.className).toContain('bg-background/70');
    expect(root.className).not.toContain('pointer-events-none');
  });

  it('overlay="passive"：透明不遮挡、不锁交互，面板本身仍可交互', () => {
    render(<PlaygroundLoading loadingMessage="识别中" isImageMode={false} overlay="passive" />);
    const root = screen.getByTestId('playground-loading');
    expect(root.getAttribute('data-overlay')).toBe('passive');
    expect(root.className).toContain('pointer-events-none');
    expect(root.className).not.toContain('backdrop-blur-[2px]');
    expect(root.className).not.toContain('bg-background/70');
    const panel = root.firstElementChild as HTMLElement;
    expect(panel.className).toContain('pointer-events-auto');
  });

  it('passive 仍保留进度与取消入口', () => {
    const onCancel = vi.fn();
    render(
      <PlaygroundLoading
        loadingMessage="识别中"
        isImageMode={false}
        overlay="passive"
        onCancel={onCancel}
      />,
    );
    expect(screen.getByTestId('playground-loading-timer')).toBeTruthy();
    const cancel = screen.getByRole('button');
    fireEvent.click(cancel);
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});

describe('端到端档位链（AC5 首条：单页/文本件仍 blocking）', () => {
  it('多页扫描件：提前切段 + 遮罩降级 = passive', () => {
    const progressive = shouldEnterPreviewDuringRecognition({
      pageCount: 10,
      isScanned: true,
      fileType: 'pdf',
    });
    expect(progressive).toBe(true);
    expect(overlayModeFor(progressive)).toBe('passive');
  });

  it('单页扫描件：不提前切段 ⇒ 仍 blocking', () => {
    const progressive = shouldEnterPreviewDuringRecognition({
      pageCount: 1,
      isScanned: true,
      fileType: 'pdf',
    });
    expect(overlayModeFor(progressive)).toBe('blocking');
  });

  it('多页文本 PDF（非扫描）：不提前切段 ⇒ 仍 blocking', () => {
    const progressive = shouldEnterPreviewDuringRecognition({
      pageCount: 10,
      isScanned: false,
      fileType: 'pdf',
    });
    expect(overlayModeFor(progressive)).toBe('blocking');
  });
});
