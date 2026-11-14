// @vitest-environment jsdom

// Copyright 2026 LexMask Contributors

// Issue#264：BatchRoute 放行白名单契约。直挂真实 BatchRoute（经 router.tsx 导出，
// 纯导出零行为差异提交后落地的判据载体）。用例源 = 单一来源常量枚举 ∪ 导航侧产物
// 抽取（buildBatchWorkbenchUrl 对四种 batch job_type 的输出经正则提取 mode）——
// 后者保证白名单常量被收窄时（AC3 变异）本契约仍因导航侧产物被弹回而变红，
// 而不是用例集合随常量缩减而假绿。
// 反自指判据（AC1）：本文件不得复刻白名单比较、不得出现模式白名单字符串字面量
// （grep 自检 = 0）；用例数据全部来自 import 的 BATCH_WIZARD_MODES 与导航产物；
// 负例 'bogus' 不属白名单集合，豁免。

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BATCH_WIZARD_MODES } from '@/constants/batch-wizard-modes';
import { buildBatchWorkbenchUrl } from '@/utils/jobPrimaryNavigation';
import { BatchRoute } from './router';

vi.mock('./features/batch', () => ({
  Batch: () => <div data-testid="wizard" />,
  BatchHub: () => <div data-testid="hub-stub" />,
}));

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="probe" data-to={`${location.pathname}${location.search}`} />;
}

// 后端 job_type 域词汇（backend/app/services/job_models.py CHECK 约束），
// 属测试输入域、非本契约被禁的模式白名单复刻。
const BATCH_JOB_TYPES = ['text_batch', 'image_batch', 'smart_batch', 'agent_batch'] as const;

function modeFromBatchUrl(to: string): string {
  const match = /^\/batch\/([a-z]+)\?/.exec(to);
  if (!match) throw new Error(`导航产物应为 /batch/<mode> 形态：${to}`);
  return match[1];
}

// 用例源并集：常量枚举 ∪ 导航侧产物（按 mode 去重，常量源优先）。
const MODE_CASES: Array<{ source: string; mode: string }> = (() => {
  const byMode = new Map<string, string>();
  for (const mode of BATCH_WIZARD_MODES) byMode.set(mode, `常量白名单:${mode}`);
  for (const jobType of BATCH_JOB_TYPES) {
    const to = buildBatchWorkbenchUrl('j-nav', jobType, 4, undefined, null);
    const mode = modeFromBatchUrl(to);
    if (!byMode.has(mode)) byMode.set(mode, `导航侧产物:${jobType}`);
  }
  return [...byMode.entries()].map(([mode, source]) => ({ mode, source }));
})();

function renderBatchRoute(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <Routes>
        <Route path="/batch" element={<div data-testid="hub" />} />
        <Route path="/batch/:batchMode" element={<BatchRoute />} />
      </Routes>
      <LocationProbe />
    </MemoryRouter>,
  );
}

describe('Issue#264：BatchRoute 放行白名单（导航产物必须命中路由白名单）', () => {
  afterEach(() => cleanup());

  it.each(MODE_CASES)('$source → /batch/$mode 放行且 query 原样保持', async ({ mode }) => {
    renderBatchRoute(`/batch/${mode}?jobId=j-1&step=4`);
    expect(await screen.findByTestId('wizard')).toBeTruthy();
    expect(screen.getByTestId('probe').getAttribute('data-to')).toBe(
      `/batch/${mode}?jobId=j-1&step=4`,
    );
    expect(screen.queryByTestId('hub')).toBeNull();
  });

  it('非法模式弹回 /batch 且 query 丢失（白名单保持闭合）', async () => {
    renderBatchRoute('/batch/bogus?jobId=j-1&step=4');
    expect(await screen.findByTestId('hub')).toBeTruthy();
    expect(screen.queryByTestId('wizard')).toBeNull();
    expect(screen.getByTestId('probe').getAttribute('data-to')).toBe('/batch');
  });
});
