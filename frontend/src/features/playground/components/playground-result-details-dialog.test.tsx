// Copyright 2026 LexMask Contributors

// Issue#311：结果页「详情」弹窗单测——质量报告 + 处理详情合并渲染、空态、失败变体、关闭态。
// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { t } from '@/i18n';
import { PlaygroundResultDetailsDialog } from './playground-result-details-dialog';
import type { PlaygroundProcessingDetailsState } from './processing-details-card';

const REPORT = {
  total_entities: 10,
  redacted_entities: 7,
  coverage_rate: 70,
  entity_type_distribution: { PERSON: 6 },
  confidence_distribution: { high: 7, medium: 3 },
  source_distribution: { has: 6, regex: 4 },
  redaction_mode: 'mask',
};

function processingState(
  overrides: Partial<PlaygroundProcessingDetailsState> = {},
): PlaygroundProcessingDetailsState {
  return {
    performance: {
      recognition: {
        duration_ms: 1000,
        failed_pages: { '3': 'timeout' },
        pages: { '1': { status: 'completed' } },
      },
    },
    mergedPerf: null,
    rows: [],
    totalMs: 1000,
    textDirect: false,
    failedPages: [3],
    hasData: true,
    jobId: 'job-1',
    filePageCount: null,
    failed: false,
    errorMessage: null,
    ...overrides,
  };
}

describe('Issue#311 详情弹窗', () => {
  it('处理方式四枚举与来源标签按旧口径映射（门⑥ C1/C2）', () => {
    const cases: Array<[string, string]> = [
      ['structured', t('mode.structured')],
      ['smart', t('mode.smart')],
      ['mask', t('mode.mask')],
      ['pseudonym', t('playground.processingModeReplace')],
    ];
    for (const [mode, label] of cases) {
      const { unmount } = render(
        <PlaygroundResultDetailsDialog
          open
          onOpenChange={() => undefined}
          report={{ ...REPORT, redaction_mode: mode }}
          processing={processingState()}
        />,
      );
      const text = screen.getByTestId('playground-quality-report').textContent ?? '';
      expect(text).toContain(label);
      expect(text).not.toContain('playground.processingModeReplace');
      unmount();
    }
  });

  it('来源 llm 显示语义模型而非原始枚举（门⑥ M4 缺口回归钉）', () => {
    render(
      <PlaygroundResultDetailsDialog
        open
        onOpenChange={() => undefined}
        report={{ ...REPORT, source_distribution: { llm: 2 } }}
        processing={processingState()}
      />,
    );
    const text = screen.getByTestId('playground-quality-report').textContent ?? '';
    expect(text).toContain(t('playground.sourceAi'));
    expect(text).not.toContain('llm');
  });

  it('redaction_mode 为空时处理方式整行不渲染（门⑥ M9 缺口回归钉）', () => {
    render(
      <PlaygroundResultDetailsDialog
        open
        onOpenChange={() => undefined}
        report={{ ...REPORT, redaction_mode: undefined }}
        processing={processingState()}
      />,
    );
    expect(screen.getByTestId('playground-quality-report').textContent).not.toContain(
      t('playground.processingMode'),
    );
  });

  it('来源 has 显示 HaS、类型标签走翻译而非原始键（门⑥ C2/I1）', () => {
    render(
      <PlaygroundResultDetailsDialog
        open
        onOpenChange={() => undefined}
        report={{ ...REPORT, source_distribution: { has: 6 } }}
        processing={processingState()}
      />,
    );
    const text = screen.getByTestId('playground-quality-report').textContent ?? '';
    expect(text).toContain('HaS');
    expect(text).not.toContain('PERSON');
  });


  it('open 时同时渲染质量报告指标与处理详情（总耗时/失败页）', () => {
    render(
      <PlaygroundResultDetailsDialog
        open
        onOpenChange={() => undefined}
        report={REPORT}
        processing={processingState()}
      />,
    );

    const report = screen.getByTestId('playground-quality-report');
    expect(report.textContent).toContain(t('playground.totalEntities'));
    expect(report.textContent).toContain('10');
    expect(report.textContent).toContain(t('playground.unprocessedEntities'));
    expect(report.textContent).toContain('3');
    expect(screen.getByTestId('processing-details-total').textContent).toContain(
      t('processingDetails.total'),
    );
    expect(screen.getByTestId('processing-details-failed-pages').textContent).toContain('3');
  });

  it('无报告且无处理详情时显示空态文案', () => {
    render(
      <PlaygroundResultDetailsDialog
        open
        onOpenChange={() => undefined}
        report={null}
        processing={processingState({ performance: null, totalMs: null, failedPages: [], hasData: false })}
      />,
    );
    expect(screen.getByTestId('playground-details-empty').textContent).toContain(
      t('playground.detailsEmpty'),
    );
    expect(screen.queryByTestId('playground-quality-report')).toBeNull();
  });

  it('任务失败时显示失败变体，不渲染阶段行', () => {
    render(
      <PlaygroundResultDetailsDialog
        open
        onOpenChange={() => undefined}
        report={null}
        processing={processingState({
          performance: null,
          totalMs: null,
          failedPages: [],
          hasData: false,
          failed: true,
          errorMessage: '识别服务不可用',
        })}
      />,
    );
    expect(screen.getByTestId('processing-details-failed').textContent).toContain(
      '识别服务不可用',
    );
    expect(screen.queryByTestId('processing-details-rows')).toBeNull();
  });

  it('默认 open=false 时 Portal 不挂载任何内容', () => {
    render(
      <PlaygroundResultDetailsDialog
        open={false}
        onOpenChange={() => undefined}
        report={REPORT}
        processing={processingState()}
      />,
    );
    expect(screen.queryByTestId('playground-details-dialog')).toBeNull();
    expect(screen.queryByTestId('playground-quality-report')).toBeNull();
  });
});