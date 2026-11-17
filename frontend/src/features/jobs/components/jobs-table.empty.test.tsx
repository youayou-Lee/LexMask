// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { JobsTable } from './jobs-table';

/**
 * Issue#351 B2：任务中心空态分流（修「页签徽标 N vs 空表」困惑）。
 *
 * 根因口径：页签徽标/统计卡为全局跨状态口径，表体按当前 tab 过滤——默认
 * 「进行中」下 0 行但徽标 7 属口径设计；空态必须分流并给出去处。
 * 数字口径钉死为**工单单位**（awaiting_review_jobs），保证「分流文案数字 ==
 * 点击后批量工单行数」可对账（items 是文件单位，冷审修订）。
 */

function renderTable(overrides: Partial<Parameters<typeof JobsTable>[0]> = {}) {
  const onTabChange = overrides.onTabChange ?? vi.fn();
  const view = render(
    <JobsTable
      rows={[]}
      loading={false}
      refreshing={false}
      pageSize={10}
      deletingJobId={null}
      tableBusy={false}
      onDelete={vi.fn()}
      {...overrides}
    />,
  );
  return { onTabChange, view };
}

const statsSevenReview = {
  totalJobs: 7,
  activeJobs: 0,
  awaitingReviewJobs: 7,
  completedJobs: 0,
  riskJobs: 0,
};

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe('Issue#351 B2：任务中心空态分流', () => {
  it('过滤态空态且其他状态有任务：分流文案 + 查看待复核按钮，点击切内层 tab', () => {
    const onTabChange = vi.fn();
    renderTable({
      tab: 'active',
      onTabChange,
      stats: statsSevenReview,
    });
    expect(screen.getByTestId('jobs-table-empty-filtered')).not.toBeNull();
    expect(screen.getByText(/当前筛选下暂无任务/)).not.toBeNull();
    // 数字与落地内容同单位（工单），文案数字 == 点击后批量工单行数
    expect(screen.getByText(/待复核 7/)).not.toBeNull();
    // EmptyState.action 不透传 testid，按按钮文本定位（文案即合同）
    fireEvent.click(screen.getByText('查看待复核'));
    expect(onTabChange).toHaveBeenCalledWith('awaiting_review');
    // 全局空态文案不应同时出现
    expect(screen.queryByText(/从批量任务入口创建后/)).toBeNull();
  });

  it('过滤态空态且其他状态也无任务：回落全局空态', () => {
    renderTable({
      tab: 'awaiting_review',
      onTabChange: vi.fn(),
      stats: {
        totalJobs: 0,
        activeJobs: 0,
        awaitingReviewJobs: 0,
        completedJobs: 0,
        riskJobs: 0,
      },
    });
    expect(screen.getByTestId('jobs-table-empty')).not.toBeNull();
    expect(screen.queryByTestId('jobs-table-empty-filtered')).toBeNull();
    expect(screen.getByText(/暂无任务记录/)).not.toBeNull();
  });

  it('stats 未传（向后兼容旧调用方）：保持全局空态不炸', () => {
    renderTable({ tab: 'active', onTabChange: vi.fn() });
    expect(screen.getByTestId('jobs-table-empty')).not.toBeNull();
    expect(screen.queryByTestId('jobs-table-empty-filtered')).toBeNull();
  });

  it('当前 tab 自身状态不计入「其他状态」：待复核页签下 0 行且无其他任务 → 全局空态', () => {
    renderTable({
      tab: 'awaiting_review',
      onTabChange: vi.fn(),
      stats: {
        totalJobs: 3,
        activeJobs: 0,
        awaitingReviewJobs: 3,
        completedJobs: 0,
        riskJobs: 0,
      },
    });
    // 3 个待复核工单都在（但 rows 空说明数据未到/分页异常）——分流不显示
    // 「待复核 N」自身误导项，也无其他状态可去 → 回落全局空态
    expect(screen.queryByTestId('jobs-table-empty-filtered')).toBeNull();
    expect(screen.getByTestId('jobs-table-empty')).not.toBeNull();
  });
});
