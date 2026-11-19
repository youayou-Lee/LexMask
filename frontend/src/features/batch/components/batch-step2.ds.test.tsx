// @vitest-environment jsdom
// Copyright 2026 LexMask Contributors

// Issue#355 Agent C：批量向导第 2 步 ds 聚焦卡钉（真稿 ios-batch-wizard 第 2 步：
// 一步一屏聚焦卡 = 步骤头 + 拖拽位 + 文件行 + 摘要块 + 左右分位动作条）。
// 全部既有 testid/行为契约（drop-zone / upload-issues / step2-row-* / step2-prev /
// step2-next / step2-clear-all）在 ds 分支保持同值；classic 分支 DOM 零漂移。
// 摘要只做有数据源的两格（文件数/总大小）；预估时长无数据源不做假数（宁缺勿谎）。

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';
import { useI18n } from '@/i18n';
import { zh } from '@/i18n/zh';
import { useUiTheme } from '@/theme/ui-theme';
import { BatchStep2Upload } from './batch-step2-upload';
import type { BatchRow, BatchUploadIssue } from '../types';

const PENDING_I18N_KEYS: Record<string, string> = {
  'batchWizard.step2.headTitle': '上传文件',
  'batchWizard.step2.summaryFiles': '文件',
  'batchWizard.step2.summarySize': '总大小',
  'batchWizard.step2.filesUnit': '个',
  'batchWizard.step2.lockedTip': '方案与识别项在上一步已配置；下一步直接开始识别',
};

const ROWS: BatchRow[] = [
  {
    file_id: 'f1',
    original_filename: '第四卷.pdf',
    file_size: 18.2 * 1024 * 1024,
    file_type: 'pdf',
  } as BatchRow,
  {
    file_id: 'f2',
    original_filename: '执行裁定书-合并.docx',
    file_size: 104 * 1024,
    file_type: 'docx',
  } as BatchRow,
];

const NO_ISSUES: BatchUploadIssue[] = [];

function makeProps(overrides: Partial<Parameters<typeof BatchStep2Upload>[0]> = {}) {
  return {
    mode: 'smart' as const,
    activeJobId: 'j-1',
    rows: ROWS,
    loading: false,
    isDragActive: false,
    getRootProps: () => ({}),
    getInputProps: (() => ({})) as ReturnType<typeof import('react-dropzone').useDropzone>['getInputProps'],
    uploadIssues: NO_ISSUES,
    uploadProgress: null,
    clearUploadIssues: vi.fn(),
    goStep: vi.fn(),
    removeRow: vi.fn().mockResolvedValue(undefined),
    clearRows: vi.fn().mockResolvedValue(undefined),
    ...overrides,
  };
}

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
  useUiTheme.setState({ theme: 'ds' });
  for (const [key, value] of Object.entries(PENDING_I18N_KEYS)) {
    if (!(key in zh)) (zh as Record<string, string>)[key] = value;
  }
});

afterEach(() => {
  cleanup();
  useUiTheme.setState({ theme: 'classic' });
});

describe('Issue#355 批量第 2 步 ds：一步一屏聚焦卡', () => {
  it('聚焦卡结构：步骤头 + 拖拽位(drop-zone) + 文件行(step2-row-*) + 摘要块 + 动作条', () => {
    render(
      <MemoryRouter>
        <BatchStep2Upload {...makeProps()} />
      </MemoryRouter>,
    );

    // 步骤头
    expect(screen.getByTestId('batch-step2-card')).not.toBeNull();
    expect(screen.getByText('上传文件')).toBeTruthy();
    // 拖拽位（既有 testid）
    expect(screen.getByTestId('drop-zone')).not.toBeNull();
    // 文件行（既有 testid 同值）
    expect(screen.getByTestId('step2-row-f1')).not.toBeNull();
    expect(screen.getByTestId('step2-row-f2')).not.toBeNull();
    // 摘要块：文件 2 个 / 总大小 18.3 MB
    expect(screen.getByTestId('batch-step2-summary').textContent).toContain('文件');
    expect(screen.getByTestId('batch-step2-summary').textContent).toContain('18.3 MB');
    // 动作条：上一步 ghost + 下一步（既有 testid/文案）
    expect(screen.getByTestId('step2-prev').textContent).toContain('上一步');
    expect(screen.getByTestId('step2-next').textContent).toContain('下一步：批量识别');
    expect(screen.getByTestId('batch-step2-actionbar')).not.toBeNull();
    // 工单关联行保留
    expect(screen.getByText('当前任务工单')).toBeTruthy();
  });

  it('动作行为不变：下一步在队列空时禁用、上一步 goStep(1)、下一步 goStep(3)', () => {
    const goStep = vi.fn();
    const { rerender } = render(
      <MemoryRouter>
        <BatchStep2Upload {...makeProps({ goStep })} />
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByTestId('step2-prev'));
    fireEvent.click(screen.getByTestId('step2-next'));
    expect(goStep).toHaveBeenNthCalledWith(1, 1);
    expect(goStep).toHaveBeenNthCalledWith(2, 3);

    rerender(
      <MemoryRouter>
        <BatchStep2Upload {...makeProps({ goStep, rows: [] })} />
      </MemoryRouter>,
    );
    expect((screen.getByTestId('step2-next') as HTMLButtonElement).disabled).toBe(true);
  });

  it('上传异常块与清理按钮保留（upload-issues / step2-clear-all 同 testid）', async () => {
    const clearRows = vi.fn().mockResolvedValue(undefined);
    window.confirm = vi.fn(() => true);
    render(
      <MemoryRouter>
        <BatchStep2Upload
          {...makeProps({
            clearRows,
            uploadIssues: [
              { id: 'i1', filename: 'bad.exe', reason: 'unsupported' },
            ] as unknown as BatchUploadIssue[],
          })}
        />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('upload-issues')).not.toBeNull();
    fireEvent.click(screen.getByTestId('step2-clear-all'));
    await waitFor(() => expect(clearRows).toHaveBeenCalled());
  });

  it('classic 分支 DOM 零漂移：无聚焦卡钩子，双栏队列卡结构保留', () => {
    useUiTheme.setState({ theme: 'classic' });
    render(
      <MemoryRouter>
        <BatchStep2Upload {...makeProps()} />
      </MemoryRouter>,
    );
    expect(screen.queryByTestId('batch-step2-card')).toBeNull();
    expect(screen.queryByTestId('batch-step2-summary')).toBeNull();
    expect(screen.queryByTestId('batch-step2-actionbar')).toBeNull();
    // 既有队列卡标题仍在（原右侧卡）
    expect(screen.getByText('上传队列')).toBeTruthy();
    expect(screen.getByTestId('drop-zone')).not.toBeNull();
  });
});
