// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#280 · F2：人工补框对话框。
//
// 钉住：
//  1) 页图经 authFetch 加载（URL 含 page 参数、**不带 raw=true**——默认净化副本
//     即识别实际吃到的页图，Issue#236 裁定）；
//  2) 框空时「保存补框」禁用（Ruling：框必填、文本可选）；
//  3) 提交成功回调 onVerified（父级刷新 page-status）并关闭对话框；
//  4) 后端 409 错误码路由到人话文案（manualError.*，未知码回落通用文案）。

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
  authFetch: vi.fn(),
  manualVerifyPage: vi.fn(),
}));

vi.mock('@/services/api-client', async () => {
  const actual = await vi.importActual<typeof import('@/services/api-client')>(
    '@/services/api-client',
  );
  return { ...actual, authFetch: mocks.authFetch };
});

vi.mock('@/services/jobsApi', () => ({
  manualVerifyPage: mocks.manualVerifyPage,
}));

import { ManualAnnotateDialog } from './manual-annotate-dialog';

afterEach(() => cleanup());

// jsdom 无 ResizeObserver（ImageBBoxEditor 的 useImageViewport 需要）——哑实现。
class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

beforeEach(() => {
  vi.stubGlobal('ResizeObserver', FakeResizeObserver);
  window.URL.createObjectURL = vi.fn().mockReturnValue('blob:mock-image');
  // 1×1 PNG blob
  const blob = new Blob([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], { type: 'image/png' });
  mocks.authFetch.mockResolvedValue({
    ok: true,
    blob: async () => blob,
  });
});

function renderDialog(props: Partial<Parameters<typeof ManualAnnotateDialog>[0]> = {}) {
  return render(
    <ManualAnnotateDialog
      jobId="job-280"
      itemId="item-280"
      fileId="file-280"
      page={5}
      open
      onOpenChange={() => {}}
      onVerified={vi.fn()}
      initialBoxes={[
        { id: 'seed-1', x: 0.1, y: 0.1, width: 0.3, height: 0.1, type: 'PERSON', selected: true },
      ]}
      {...props}
    />,
  );
}

describe('人工补框对话框（Issue#280）', () => {
  it('页图经 authFetch 加载：URL 带 page 参数、不带 raw=true', async () => {
    renderDialog();
    await waitFor(() => {
      expect(mocks.authFetch).toHaveBeenCalled();
    });
    const url = String(mocks.authFetch.mock.calls[0][0]);
    expect(url).toContain('/files/file-280/page-image');
    expect(url).toContain('page=5');
    expect(url).not.toContain('raw=true');
  });

  it('框空时保存禁用；有框时可用', async () => {
    // 框空（初始播种为空数组）：保存禁用
    renderDialog({ initialBoxes: [] });
    await waitFor(() => {
      expect(screen.getByTestId('manual-annotate-save')).toBeTruthy();
    });
    expect(screen.getByTestId('manual-annotate-save').getAttribute('disabled')).not.toBeNull();
    cleanup();

    // 有框（初始播种一个框）：保存可用
    renderDialog();
    await waitFor(() => {
      const save = screen.getByTestId('manual-annotate-save');
      expect(save.getAttribute('disabled')).toBeNull();
    });
  });

  it('提交成功：携带框调 manualVerifyPage → onVerified(page)', async () => {
    const onVerified = vi.fn();
    const onOpenChange = vi.fn();
    mocks.manualVerifyPage.mockResolvedValue({
      page: 5,
      state: 'manual_verified',
      box_count: 1,
      verified_at: 't',
      operator: 'op',
      item_status: 'awaiting_review',
    });
    renderDialog({ onVerified, onOpenChange });
    await waitFor(() => {
      expect(screen.getByTestId('manual-annotate-save').getAttribute('disabled')).toBeNull();
    });
    fireEvent.click(screen.getByTestId('manual-annotate-save'));
    await waitFor(() => {
      expect(mocks.manualVerifyPage).toHaveBeenCalledWith('job-280', 'item-280', 5, [
        expect.objectContaining({ type: 'PERSON', x: 0.1, y: 0.1, width: 0.3, height: 0.1 }),
      ]);
      expect(onVerified).toHaveBeenCalledWith(5);
      expect(onOpenChange).toHaveBeenCalledWith(false);
    });
  });

  it('409 manual_boxes_conflict 路由到人话文案；未知码回落通用文案', async () => {
    const { ApiError } = await import('@/services/api-client');
    mocks.manualVerifyPage.mockRejectedValue(
      new ApiError('conflict', 409, 'unknown', 'manual_boxes_conflict'),
    );
    renderDialog();
    await waitFor(() => {
      expect(screen.getByTestId('manual-annotate-save').getAttribute('disabled')).toBeNull();
    });
    fireEvent.click(screen.getByTestId('manual-annotate-save'));
    await waitFor(() => {
      expect(screen.getByTestId('manual-annotate-error')).toBeTruthy();
    });
    expect(screen.getByTestId('manual-annotate-error').textContent).toContain('审阅画布');

    cleanup();
    mocks.manualVerifyPage.mockRejectedValue(new ApiError('weird', 400, 'unknown', 'weird_code'));
    renderDialog();
    await waitFor(() => {
      expect(screen.getByTestId('manual-annotate-save').getAttribute('disabled')).toBeNull();
    });
    fireEvent.click(screen.getByTestId('manual-annotate-save'));
    await waitFor(() => {
      expect(screen.getByTestId('manual-annotate-error')).toBeTruthy();
    });
    expect(screen.getByTestId('manual-annotate-error').textContent.length).toBeGreaterThan(0);
  });
});
