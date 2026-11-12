// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom
// Issue#274 AC6：PlaygroundLoading 阶段条（stagesView 叠加，不替换既有元素）。
// - stagesView 注入 ⇒ 阶段行渲染（①✓→②当前）+ 文案 + ETA；
// - 无 stagesView（parse 窗口/旧任务）⇒ 不渲染阶段行，既有渲染不变（零回归面）；
// - 加速器并行段附注「不影响逐页进度」。
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { PlaygroundLoading } from './playground-loading';
import type { StagesView } from '../lib/task-stages';

afterEach(cleanup);

const base = {
  loadingMessage: '正在识别「demo.pdf」',
  isImageMode: false,
};

const PREFLIGHT: StagesView = {
  current: 'preflight',
  done: [],
  acceleratorRunning: false,
  etaSec: null,
  perPageSec: null,
};

const PAGE_LOOP: StagesView = {
  current: 'page_loop',
  done: ['preflight'],
  acceleratorRunning: false,
  etaSec: 90,
  perPageSec: null,
};

describe('Issue#274 PlaygroundLoading 阶段条（AC6）', () => {
  it('stagesView 注入 ⇒ 阶段行渲染：① 文档预检（乐观/账面 running）', () => {
    render(<PlaygroundLoading {...base} stagesView={PREFLIGHT} />);
    const bar = screen.getByTestId('playground-loading-stages');
    expect(bar).toBeTruthy();
    expect(screen.getByTestId('playground-loading-stage-preflight').textContent).toContain(
      '文档预检',
    );
    expect(screen.getByTestId('playground-loading-stages').textContent).toContain(
      '正在做文档预检',
    );
  });

  it('② 逐页识别：preflight ✓ + 当前段 + ETA 后缀（约 Xs）', () => {
    render(<PlaygroundLoading {...base} stagesView={PAGE_LOOP} />);
    const bar = screen.getByTestId('playground-loading-stages');
    expect(screen.getByTestId('playground-loading-stage-preflight').textContent).toContain(
      '文档预检',
    );
    expect(screen.getByTestId('playground-loading-stage-page_loop').textContent).toContain(
      '逐页识别',
    );
    expect(screen.getByTestId('playground-loading-eta').textContent).toContain('约');
    expect(bar.textContent).toContain('正在逐页识别');
  });

  it('加速器并行段（②'+'）附注「不影响逐页进度」且 current 仍是逐页识别', () => {
    render(
      <PlaygroundLoading
        {...base}
        stagesView={{ ...PAGE_LOOP, acceleratorRunning: true }}
      />,
    );
    expect(
      screen.getByTestId('playground-loading-stage-accelerator-running').textContent,
    ).toContain('不影响逐页进度');
    expect(screen.getByTestId('playground-loading-stage-page_loop')).toBeTruthy();
  });

  it('无 stagesView（parse 窗口/旧任务）⇒ 不渲染阶段行（既有渲染零回归）', () => {
    render(<PlaygroundLoading {...base} />);
    expect(screen.queryByTestId('playground-loading-stages')).toBeNull();
    // 既有元素仍在。
    expect(screen.getByTestId('playground-loading-progress')).toBeTruthy();
  });

  it('乐观①与账面②同框不成立（门控互斥）：注入 preflight 时不渲染 page_loop', () => {
    render(<PlaygroundLoading {...base} stagesView={PREFLIGHT} />);
    expect(screen.queryByTestId('playground-loading-stage-page_loop')).toBeNull();
  });
});
