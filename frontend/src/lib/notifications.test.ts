// Copyright 2026 LexMask Contributors

import { describe, expect, it, vi, afterEach } from 'vitest';
import { showToast } from '@/components/Toast';
import { notifyDone, phaseJustFinished } from './notifications';

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));

describe('phaseJustFinished', () => {
  it('fires exactly on the >0 -> 0 transition', () => {
    expect(phaseJustFinished(5, 0)).toBe(true);
    expect(phaseJustFinished(1, 0)).toBe(true);
  });

  it('does not fire while work remains or when idle', () => {
    expect(phaseJustFinished(5, 3)).toBe(false);
    expect(phaseJustFinished(0, 0)).toBe(false);
    // 新任务开跑（0 -> N）不是完成
    expect(phaseJustFinished(0, 4)).toBe(false);
  });
});

// Issue#194 终审 D（AC2）：任务完成通知可点击——toast 携带 onClick（点击直达
// /single?file_id=），无 onClick 的旧调用兼容。系统通知降级为不可点击（浏览器
// Notification 的 onclick 接不进 SPA 路由，取舍见 notifyDone 注释）。
describe('notifyDone onClick（Issue#194 终审 D）', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('携带 onClick 时 toast 收到 { onClick }（第三参）', () => {
    const onClick = () => {};
    notifyDone('打码完成：a.pdf', undefined, onClick);
    expect(showToast).toHaveBeenCalledWith('打码完成：a.pdf', 'success', { onClick });
  });

  it('有 body 时标题拼接照旧，onClick 照传', () => {
    const onClick = () => {};
    notifyDone('识别完成：a.pdf', '3 页', onClick);
    expect(showToast).toHaveBeenCalledWith('识别完成：a.pdf：3 页', 'success', { onClick });
  });

  it('无 onClick（旧调用）⇒ 第三参 undefined，不破坏既有 toast 形状', () => {
    notifyDone('处理失败：a.pdf');
    expect(showToast).toHaveBeenCalledWith('处理失败：a.pdf', 'success', undefined);
  });
});
