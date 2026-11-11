// Copyright 2026 LexMask Contributors

// Issue#282：历史/处理结果页的「供料」来源页签与供料行入口。
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { useT } from '@/i18n';
import { buildJobPrimaryNavigationLabels } from '@/utils/jobPrimaryNavigation';
import type { FileListItem } from '@/types';

import { HistoryDataRow } from './history-row';
import { HistoryFilterMenu } from './history-filters';

afterEach(cleanup);

function RowHarness({ row }: { row: FileListItem }) {
  const t = useT();
  return (
    <MemoryRouter>
      <ul>
        <HistoryDataRow
          row={row}
          selected={false}
          density={{
            table: 'min-w-[1080px]',
            rowHeight: 44,
            rowPaddingY: 3,
            rowPaddingX: 8,
            filenameSkeletonHeight: 12,
            statusDetailMaxWidth: 180,
            skeletonButtonSize: 24,
            skeletonButtonRadius: 8,
          }}
          navLabels={buildJobPrimaryNavigationLabels(t)}
          t={t}
          onToggle={() => {}}
          onDownload={() => {}}
          onDelete={() => {}}
          onCompare={() => {}}
        />
      </ul>
    </MemoryRouter>
  );
}

const agentMdRow: FileListItem = {
  file_id: 'f-agent',
  original_filename: '案卷_脱敏MD_20261112_1200_abcd.md',
  file_size: 100,
  file_type: 'txt' as FileListItem['file_type'],
  created_at: '2026-11-12T04:00:00',
  has_output: true,
  entity_count: 2,
  upload_source: 'agent_md',
  agent_md_task_id: 't-42',
};

describe('history agent_md source', () => {
  it('筛选弹层渲染四个来源页签，切到供料时回传 agent_md', () => {
    const onSourceTabChange = vi.fn();
    render(
      <HistoryFilterMenu
        sourceTab="all"
        onSourceTabChange={onSourceTabChange}
        dateFilter="all"
        onDateFilterChange={() => {}}
        fileTypeFilter="all"
        onFileTypeFilterChange={() => {}}
        statusFilter="all"
        onStatusFilterChange={() => {}}
        hasActiveFilter={false}
        onClearFilters={() => {}}
      />,
    );
    fireEvent.click(screen.getByText('筛选'));
    for (const name of ['all', 'playground', 'batch', 'agent_md']) {
      expect(screen.getByTestId(`source-tab-${name}`)).toBeTruthy();
    }
    const tab = screen.getByTestId('source-tab-agent_md');
    fireEvent.mouseDown(tab);
    fireEvent.click(tab);
    expect(onSourceTabChange).toHaveBeenCalledWith('agent_md');
  });

  it('供料行给「打开结果」深链并隐藏对比', () => {
    render(<RowHarness row={agentMdRow} />);
    const open = screen.getByTestId('open-agent-md-f-agent');
    expect(open.getAttribute('href')).toBe('/agent-md?taskId=t-42');
    expect(screen.queryByTestId('compare-f-agent')).toBeNull();
  });

  it('非供料行不出现「打开结果」', () => {
    render(
      <RowHarness row={{ ...agentMdRow, upload_source: 'playground', agent_md_task_id: null }} />,
    );
    expect(screen.queryByTestId('open-agent-md-f-agent')).toBeNull();
    expect(screen.getByTestId('compare-f-agent')).toBeTruthy();
  });
});
