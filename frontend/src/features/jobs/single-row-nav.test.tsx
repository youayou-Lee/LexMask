// Copyright 2026 LexMask Contributors

// Issue#194 Task 12：任务中心 single 行——类型徽章 + 按状态直达识别页。
//
// 钉住的不变量：
// 1) single 行类型徽章渲染 jobs.type.single（zh「单文件」/ en 'Single file'），
//    batch 行仍是 jobs.batchTask；
// 2) single 行主操作按钮按状态直达 /single?file_id=<config.single_file_id>——
//    awaiting_review / completed / failed / 识别中 全部进同一识别页入口，
//    具体状态视图（识别中重挂/确认/结果/失败）由识别页 loadExistingFile +
//    adoptActiveTask 接管（T9-T11）；
// 3) file_id 取 job.config.single_file_id（后端 submit_single_recognition 提交时写入，
//    list 行无 items 也带它）；缺失时不出直达按钮——绝不落批量向导；
// 4) 批量四类型零漂移：text_batch awaiting_review 主操作仍是批量向导 step=4（T7 行为钉）。

// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { t } from '@/i18n';
import type { JobProgress, JobSummary } from '@/services/jobsApi';

import { JobsTable } from './components/jobs-table';

const CREATED = '2026-01-01T00:00:00Z';

const ZERO_PROGRESS: JobProgress = {
  total_items: 1,
  pending: 0,
  processing: 0,
  queued: 0,
  parsing: 0,
  ner: 0,
  vision: 0,
  awaiting_review: 0,
  review_approved: 0,
  redacting: 0,
  completed: 0,
  failed: 0,
};

let seq = 0;

function makeJob(overrides: Partial<JobSummary> = {}): JobSummary {
  seq += 1;
  return {
    id: `job-${seq}`,
    job_type: 'text_batch',
    title: `任务${seq}`,
    status: 'awaiting_review',
    skip_item_review: false,
    config: {},
    error_message: null,
    created_at: CREATED,
    updated_at: CREATED,
    progress: { ...ZERO_PROGRESS },
    nav_hints: { item_count: 1 },
    ...overrides,
  };
}

function singleJob(status: string, fileId: string | null): JobSummary {
  return makeJob({
    job_type: 'single',
    status,
    config: fileId ? { single_file_id: fileId } : {},
  });
}

function renderTable(rows: JobSummary[]) {
  return render(
    <MemoryRouter>
      <JobsTable
        rows={rows}
        loading={false}
        refreshing={false}
        pageSize={10}
        deletingJobId={null}
        tableBusy={false}
        onDelete={vi.fn()}
      />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  seq = 0;
});

afterEach(cleanup);

describe('Issue#194 T12：任务中心 single 行直达识别页', () => {
  it('single 行渲染 jobs.type.single 类型徽章，batch 行仍是 jobs.batchTask', () => {
    renderTable([makeJob(), singleJob('awaiting_review', 'file-9')]);
    const badges = screen.getAllByTestId('job-type-badge').map((el) => el.textContent);
    expect(badges).toContain(t('jobs.type.single'));
    expect(badges).toContain(t('jobs.batchTask'));
  });

  it('awaiting_review single 行主操作直达 /single?file_id=，文案沿用 jobNav.continueReview', () => {
    renderTable([makeJob(), singleJob('awaiting_review', 'file-9')]);
    const link = screen.getByTestId('job-primary-action-job-2');
    expect(link.getAttribute('href')).toBe('/single?file_id=file-9');
    expect(link.getAttribute('aria-label')).toBe(t('jobNav.continueReview'));
  });

  it('全部状态直达同一识别页入口（processing/completed/failed），文案按状态区分', () => {
    renderTable([
      singleJob('processing', 'file-a'),
      singleJob('completed', 'file-b'),
      singleJob('failed', 'file-c'),
    ]);
    expect(screen.getByTestId('job-primary-action-job-1').getAttribute('href')).toBe(
      '/single?file_id=file-a',
    );
    expect(screen.getByTestId('job-primary-action-job-2').getAttribute('href')).toBe(
      '/single?file_id=file-b',
    );
    expect(screen.getByTestId('job-primary-action-job-3').getAttribute('href')).toBe(
      '/single?file_id=file-c',
    );
    expect(screen.getByTestId('job-primary-action-job-1').getAttribute('aria-label')).toBe(
      t('jobNav.viewProgress'),
    );
    expect(screen.getByTestId('job-primary-action-job-2').getAttribute('aria-label')).toBe(
      t('jobNav.downloadRedactedResult'),
    );
    expect(screen.getByTestId('job-primary-action-job-3').getAttribute('aria-label')).toBe(
      t('jobNav.viewFailureDetail'),
    );
  });

  it('缺 single_file_id 的 single 行不出直达按钮（绝不落批量向导），详情入口仍在', () => {
    renderTable([makeJob(), singleJob('awaiting_review', null)]);
    expect(screen.queryByTestId('job-primary-action-job-2')).toBeNull();
    expect(screen.getByTestId('job-detail-link-job-2')).toBeTruthy();
  });

  it('批量四类型零漂移：text_batch awaiting_review 主操作仍指向批量向导 step=4', () => {
    const batch = makeJob({
      nav_hints: { item_count: 2, first_awaiting_review_item_id: 'item-1' },
    });
    renderTable([batch, singleJob('awaiting_review', 'file-9')]);
    const link = screen.getByTestId(`job-primary-action-${batch.id}`);
    expect(link.getAttribute('href')).toBe(
      `/batch/text?jobId=${batch.id}&step=4&itemId=item-1`,
    );
    expect(link.getAttribute('aria-label')).toBe(t('jobNav.continueReview'));
  });
});
