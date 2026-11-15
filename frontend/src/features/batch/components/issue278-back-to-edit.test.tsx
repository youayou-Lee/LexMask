// Copyright 2026 LexMask Contributors

// Issue#278「回到编辑」（§3.4/§3.7，r2 F5 重写后的接线判据）：
//  - batch：弹窗宿主（step3）常驻按钮 → 先 flushCurrentReviewDraft 再 resolveExportIssue
//    带正确 fileId，关弹窗（无 phase 导航代码在弹窗内）；
//  - playground：单文件是同页内联编辑（无 phase 可跳）——按钮收起导出块 + 锚点聚焦
//    内嵌 ImageBBoxEditor 容器，不触发任何路由/导航。
//
// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const statusBox = vi.hoisted(() => ({ current: null as ItemPageStatus | null }));

vi.mock('../hooks/use-batch-page-status', () => ({
  useBatchPageStatus: () => ({
    status: statusBox.current,
    error: null,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

vi.mock('../../playground/hooks/use-file-job-item', () => ({
  useFileJobItem: vi.fn(() => ({ jobId: 'job-1', itemId: 'item-1' })),
}));

vi.mock('@/services/jobsApi', () => ({
  getItemPageStatus: vi.fn(),
  startItemExport: vi.fn(),
  getItemExport: vi.fn(),
  listItemExports: vi.fn(),
  getItemRefByFile: vi.fn(),
  retryItemPages: vi.fn(),
  getJobExportReport: vi.fn(),
}));

import { PartialExportDialog } from '../components/partial-export-dialog';
import { PlaygroundPageExport } from '../../playground/components/playground-page-export';

function makeStatus(): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 6,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
      { page: 3, state: 'success' },
      { page: 4, state: 'failed', reason: 'x' },
      { page: 5, state: 'success' },
      { page: 6, state: 'pending' },
    ],
    failed_pages: [4],
  };
}

beforeEach(() => {
  statusBox.current = makeStatus();
});

afterEach(cleanup);

async function renderDialog(props: { onBackToEdit: () => void }) {
  await act(async () => {
    render(
      <PartialExportDialog
        open
        jobId="job-1"
        itemId="item-1"
        filename="测试卷.pdf"
        onBackToEdit={props.onBackToEdit}
        onClose={() => {}}
      />,
    );
  });
  await act(async () => {
    await Promise.resolve();
  });
}

const nextFrame = () =>
  new Promise<void>((resolve) => {
    if (typeof requestAnimationFrame === 'function') {
      requestAnimationFrame(() => resolve());
    } else {
      setTimeout(resolve, 0);
    }
  });

describe('回到编辑（Issue#278）', () => {
  it('batch：按钮触发 flush 草稿 + resolveExportIssue 带正确 fileId', async () => {
    const flush = vi.fn(async () => true);
    const resolveExportIssue = vi.fn();
    const onClose = vi.fn();

    await renderDialog({
      onBackToEdit: () => {
        void flush();
        resolveExportIssue('file-1');
        onClose();
      },
    });

    const button = screen.getByTestId('export-back-to-edit');
    fireEvent.click(button);
    expect(flush).toHaveBeenCalledTimes(1);
    expect(resolveExportIssue).toHaveBeenCalledWith('file-1');
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('未接 onBackToEdit 的宿主不渲染按钮（弹窗自持不强加导航）', async () => {
    await act(async () => {
      render(
        <PartialExportDialog
          open
          jobId="job-1"
          itemId="item-1"
          filename="测试卷.pdf"
          onClose={() => {}}
        />,
      );
    });
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByTestId('export-back-to-edit')).toBeNull();
  });

  it('playground：按钮收起导出块（同页内联，无 phase 导航）', async () => {
    const scrollSpy = vi.fn();
    const editorHost = document.createElement('div');
    editorHost.id = 'playground-bbox-editor';
    editorHost.scrollIntoView = scrollSpy;
    document.body.appendChild(editorHost);

    render(<PlaygroundPageExport fileId="file-1" />);
    // 先收起默认展开态，再点「回到编辑」验证折叠 + 锚点聚焦
    fireEvent.click(screen.getByTestId('playground-page-export-toggle'));
    expect(screen.queryByTestId('playground-page-export-submit')).toBeNull();

    fireEvent.click(screen.getByTestId('playground-page-export-toggle'));
    const button = screen.getByTestId('export-back-to-edit');
    fireEvent.click(button);
    // Issue#313：回到画布的动作延后一帧（等弹窗/滚动锁释放），测试等待 rAF。
    await act(async () => {
      await nextFrame();
    });
    // 导出块收起（提交按钮消失）+ 锚点聚焦编辑器（scrollIntoView 被调）
    expect(screen.queryByTestId('playground-page-export-submit')).toBeNull();
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    editorHost.remove();
  });

  it('playground（bare 弹窗）：回到编辑通知宿主关闭弹窗，并在下一帧回到画布', async () => {
    const onRequestClose = vi.fn();
    const scrollSpy = vi.fn();
    const editorHost = document.createElement('div');
    editorHost.id = 'playground-bbox-editor';
    editorHost.scrollIntoView = scrollSpy;
    // 真实画布是 ImageBBoxEditor 的 div[role=application][tabIndex=0]（门⑥ M-4）。
    const focusTarget = document.createElement('div');
    focusTarget.setAttribute('role', 'application');
    focusTarget.tabIndex = 0;
    const focusSpy = vi.spyOn(focusTarget, 'focus');
    editorHost.appendChild(focusTarget);
    document.body.appendChild(editorHost);

    render(<PlaygroundPageExport fileId="file-1" bare onRequestClose={onRequestClose} />);
    expect(screen.getByTestId('playground-page-export-submit')).toBeTruthy();

    fireEvent.click(screen.getByTestId('export-back-to-edit'));
    // bare 形态自己收不起来：必须把关闭动作交给宿主（父 Dialog）。
    expect(onRequestClose).toHaveBeenCalledTimes(1);

    await act(async () => {
      await nextFrame();
    });
    expect(scrollSpy).toHaveBeenCalledTimes(1);
    expect(focusSpy).toHaveBeenCalled();
    editorHost.remove();
  });
});
