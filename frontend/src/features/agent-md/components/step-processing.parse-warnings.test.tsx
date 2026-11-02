// Copyright 2026 LexMask Contributors

// Issue#212 门⑥ I4（F2）：解析告警面板必须有渲染判据——把面板条件改成 false &&
// 也必须变红，防"可观测面在 UI 上静默消失"。
//
// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { StepProcessing } from './step-processing';
import type { AgentMdStatus } from '../api';

afterEach(() => cleanup());

const BASE: AgentMdStatus = {
  task_id: 'tk-1',
  state: 'parsing',
  stage: 'mineru',
  pages_done: 0,
  pages_total: 80,
  message: '',
  warnings_count: 0,
};

function renderWith(status: AgentMdStatus) {
  // pollOnce 永不 resolve：避免 2s 轮询在同一条用例里触发状态切换
  render(
    <StepProcessing
      status={status}
      pollOnce={() => new Promise<AgentMdStatus | null>(() => {})}
      onError={vi.fn()}
      onFail={vi.fn()}
    />,
  );
}

describe('StepProcessing 解析告警面板（Issue#212）', () => {
  it('有丢弃空表时显示面板与计数', () => {
    renderWith({
      ...BASE,
      dropped_blocks: { empty_table: 2, unhandled: {}, source: 'content_list_v2' },
      warnings_total: 3,
      warnings: ['empty table block: page=0', 'empty table block: page=1'],
    });

    const panel = screen.getByTestId('agent-md-parse-warnings');
    expect(panel).toBeTruthy();
    expect(screen.getByTestId('agent-md-dropped-empty-table').textContent).toContain('2');
    expect(panel.textContent).toContain('empty table block: page=0');
  });

  it('无丢弃、无告警时不显示面板（防"永远显示"假绿）', () => {
    renderWith(BASE);
    expect(screen.queryByTestId('agent-md-parse-warnings')).toBeNull();
  });
});
