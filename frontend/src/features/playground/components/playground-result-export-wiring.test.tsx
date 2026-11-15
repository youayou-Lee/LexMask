// Copyright 2026 LexMask Contributors

// Issue#313 门⑥ I-3：result 阶段的导出块也必须把「回到编辑」接到 onBackToEdit。
// 该阶段不渲染 #playground-bbox-editor，缺这根线时按钮点了没有任何可见效果。

// @vitest-environment jsdom
import { act, fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import type { ItemPageStatus } from '@/services/jobsApi';

const mocks = vi.hoisted(() => ({
  status: null as ItemPageStatus | null,
}));

vi.mock('@/features/batch/hooks/use-batch-page-status', () => ({
  useBatchPageStatus: () => ({
    status: mocks.status,
    error: null,
    loading: false,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/features/playground/hooks/use-file-job-item', () => ({
  useFileJobItem: () => ({ jobId: 'job-1', itemId: 'item-1' }),
}));

vi.mock('@/services/jobsApi', () => ({
  getItemPageStatus: vi.fn(),
  startItemExport: vi.fn(),
  getItemExport: vi.fn(),
  listItemExports: vi.fn(),
}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, refresh: vi.fn() }),
}));

vi.mock('../hooks/use-output-page-preview', () => ({
  useOutputPagePreview: () => ({ pages: [], pageCount: 0, loading: false, error: null }),
}));

vi.mock('./playground-result-action-bar', () => ({
  PlaygroundResultActionBar: () => null,
  RedactionReportSection: () => null,
}));

vi.mock('./playground-result-views', () => ({
  TextResultView: () => null,
  ImageResultView: () => null,
  OutputResultView: () => null,
}));

vi.mock('./processing-details-card', () => ({
  PlaygroundProcessingDetails: () => null,
  usePlaygroundProcessingDetails: () => ({
    performance: null,
    mergedPerf: null,
    rows: [],
    totalMs: null,
    textDirect: false,
    failedPages: [],
    hasData: false,
    jobId: 'job-1',
    filePageCount: null,
    failed: false,
    errorMessage: null,
  }),
}));

import type { FileInfo } from '../types';
import { PlaygroundResult } from './playground-result';

function status(): ItemPageStatus {
  return {
    item_id: 'item-1',
    job_id: 'job-1',
    file_id: 'file-1',
    paged: true,
    page_count: 2,
    recognition_state: 'done',
    item_status: 'awaiting_review',
    snapshot_at: '2026-11-15T00:00:00+00:00',
    pages: [
      { page: 1, state: 'success' },
      { page: 2, state: 'success' },
    ],
    failed_pages: [],
  } as unknown as ItemPageStatus;
}

describe('result 阶段导出块接线（Issue#313 I-3）', () => {
  it('点「回到编辑」→ onBackToEdit 被调用（不是只滚画布）', async () => {
    mocks.status = status();
    const onBackToEdit = vi.fn();
    await act(async () => {
      render(
        <PlaygroundResult
          fileInfo={
            {
              file_id: 'file-1',
              filename: 'a.docx',
              file_size: 1,
              file_type: 'docx',
              is_scanned: false,
              page_count: 2,
              pages: [],
            } as unknown as FileInfo
          }
          content=""
          entities={[]}
          entityMap={{}}
          redactedCount={0}
          redactionReport={null}
          reportOpen={false}
          setReportOpen={vi.fn()}
          versionHistory={[]}
          versionHistoryOpen={false}
          setVersionHistoryOpen={vi.fn()}
          isImageMode={false}
          imageUrl=""
          currentPage={1}
          totalPages={2}
          onPageChange={vi.fn()}
          visibleBoxes={[]}
          boundingBoxes={[]}
          visionTypes={[]}
          getVisionTypeConfig={() => ({ name: '', color: '' })}
          onBackToEdit={onBackToEdit}
          onReset={vi.fn()}
          onDownload={vi.fn()}
        />,
      );
    });

    // Issue#311 起导出块默认收起：先展开摘要，再找「回到编辑」。
    fireEvent.click(screen.getByTestId('playground-page-export-toggle'));
    fireEvent.click(screen.getByTestId('export-back-to-edit'));
    expect(onBackToEdit).toHaveBeenCalledTimes(1);
  });
});
