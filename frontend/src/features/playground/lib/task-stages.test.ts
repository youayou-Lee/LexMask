// Copyright 2026 LexMask Contributors

// Issue#274：阶段状态机前端纯函数——stages 解析 / 乐观①门控 / ETA（AC5/AC6 判据）。
// 判据编号对应方案 §4（docs/plans/2026-11-13-issue274-first-screen-stages.md v3）。

import { describe, expect, it } from 'vitest';

import {
  optimisticPreflightEligible,
  stagesFromPerformance,
  stagesViewFromPerformance,
  type EtaSample,
} from './task-stages';

const PERF_STAGES = (rows: unknown[]) => ({ recognition: { stages: rows } });

const row = (name: string, state: string, extra: Record<string, unknown> = {}) => ({
  name,
  started_at: '2026-11-13T00:00:00Z',
  finished_at: state === 'running' ? null : '2026-11-13T00:00:05Z',
  state,
  detail: {},
  ...extra,
});

// ---------------------------------------------------------------------------
// stagesFromPerformance：解析 / 坏形回落
// ---------------------------------------------------------------------------
describe('Issue#274 stagesFromPerformance — stages 解析', () => {
  it('合法账 → 行数组（含 detail）', () => {
    const rows = stagesFromPerformance(
      PERF_STAGES([row('preflight', 'completed'), row('page_loop', 'running')]),
    );
    expect(rows).not.toBeNull();
    expect(rows!.map((r) => r.name)).toEqual(['preflight', 'page_loop']);
    expect(rows![1].state).toBe('running');
  });

  it('缺键 / 坏形（旧任务）→ null（调用方回落现状渲染，AC6 零回归面）', () => {
    expect(stagesFromPerformance(null)).toBeNull();
    expect(stagesFromPerformance({})).toBeNull();
    expect(stagesFromPerformance({ recognition: {} })).toBeNull();
    expect(stagesFromPerformance({ recognition: { stages: 'garbage' } })).toBeNull();
    expect(stagesFromPerformance({ recognition: { stages: [1, 'x'] } })).toBeNull();
  });

  it('清账空数组 → 空数组（非 null）：parse 窗口「以账为准」= 无阶段条', () => {
    const rows = stagesFromPerformance({ recognition: { stages: [] } });
    expect(rows).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// 乐观①门控（v2/F1 收紧 + v3 注记 1 finished_at 加固）
// ---------------------------------------------------------------------------
describe('Issue#274 optimisticPreflightEligible — 乐观门控', () => {
  const base = {
    textMode: false,
    progressStage: 'vision' as string | null,
    progressCurrent: 0,
    hasRecognitionFinishedAt: false,
    hasStageRows: false,
  };

  it('vision + current=0 + 无残账 ⇒ 可乐观显示①', () => {
    expect(optimisticPreflightEligible(base)).toBe(true);
  });

  it('parse 窗口（progress_stage 空/null）⇒ 不可（AC6-parse-a）', () => {
    expect(optimisticPreflightEligible({ ...base, progressStage: null })).toBe(false);
    expect(optimisticPreflightEligible({ ...base, progressStage: '' })).toBe(false);
    expect(optimisticPreflightEligible({ ...base, progressStage: 'ner' })).toBe(false);
  });

  it('文本件 ⇒ 不可（无页级计数语义）', () => {
    expect(optimisticPreflightEligible({ ...base, textMode: true })).toBe(false);
  });

  it('已有页完成（current>0）⇒ 不可（以账为准）', () => {
    expect(optimisticPreflightEligible({ ...base, progressCurrent: 3 })).toBe(false);
  });

  it('重跑残账（finished_at 在账）⇒ 不可（v3 注记 1 角例：preflight 失败后重跑 parse 窗口）', () => {
    expect(
      optimisticPreflightEligible({ ...base, hasRecognitionFinishedAt: true }),
    ).toBe(false);
  });

  it('stages 账非空 ⇒ 不可（账已接管，不需要乐观）', () => {
    expect(optimisticPreflightEligible({ ...base, hasStageRows: true })).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// ETA（AC5）：段标签样本源 / 缓存页剔除 / p75 / 严格单调
// ---------------------------------------------------------------------------
describe('Issue#274 ETA — 保守版（v2/F2 段标签 + v2/F10 严格单调）', () => {
  const sample = (ms: number, opts: { hit?: boolean; label?: string | null } = {}): EtaSample => ({
    durationMs: ms,
    cacheHit: opts.hit ?? false,
    stageLabel: opts.label === undefined ? '视觉识别' : opts.label,
  });

  it('样本 <3 ⇒ 无总 ETA，只给「约 N 秒/页」（中位数 ceil）', () => {
    const out = stagesViewEtaHelper([sample(700), sample(900)], {
      completed: 0,
      total: 10,
      concurrency: 2,
      previousEtaSec: null,
    });
    expect(out.etaSec).toBeNull();
    expect(out.perPageSec).toBe(1); // median 0.8s → ceil = 1
  });

  it('样本 ≥3 ⇒ ETA = ceil(剩余 × p75 / 并发)', () => {
    // 有效样本 1000..4000：p75（nearest-rank, n=6 → 第 5 小）= 3000ms
    const samples = [1000, 2000, 3000, 4000, 1500, 2500].map((ms) => sample(ms));
    const out = stagesViewEtaHelper(samples, {
      completed: 2,
      total: 12,
      concurrency: 2,
      previousEtaSec: null,
    });
    expect(out.etaSec).toBe(Math.ceil(10 * 3.0 / 2)); // 15s
  });

  it('缓存命中页被剔除（不拉低 ETA）', () => {
    const samples = [1000, 2000, 50, 3000, 4000, 2500].map((ms, i) => sample(ms, { hit: i === 2 }));
    const out = stagesViewEtaHelper(samples, {
      completed: 0,
      total: 10,
      concurrency: 2,
      previousEtaSec: null,
    });
    // 剔除 50ms 后有效样本 p75 = 3000ms（n=5 → idx 3）
    expect(out.etaSec).toBe(Math.ceil(10 * 3.0 / 2));
  });

  it('段标签过滤：只取当前段样本（v2/F2：两段式第二段不得混入第一段）', () => {
    const samples = [
      sample(700, { label: 'OCR+HaS识别' }),
      sample(800, { label: 'OCR+HaS识别' }),
      sample(3000, { label: '视觉特征识别' }),
      sample(3200, { label: '视觉特征识别' }),
      sample(3400, { label: '视觉特征识别' }),
    ];
    const out = stagesViewEtaHelper(samples, {
      completed: 0,
      total: 6,
      concurrency: 1,
      previousEtaSec: null,
      currentStageLabel: '视觉特征识别',
    });
    // 仅 3 个本段样本 → ETA = ceil(6 × 3.4 / 1) = 21s
    expect(out.etaSec).toBe(21);
  });

  it('严格单调：新算值上抬被 min 压住（v2/F10）', () => {
    const samples = [1000, 2000, 3000, 4000, 1500, 2500].map((ms) => sample(ms));
    const out = stagesViewEtaHelper(samples, {
      completed: 2,
      total: 12,
      concurrency: 2,
      previousEtaSec: 8,
    });
    expect(out.etaSec).toBe(8);
  });

  it('completed ≥ total ⇒ ETA/每页均消失', () => {
    const samples = [1000, 2000, 3000, 4000, 1500, 2500].map((ms) => sample(ms));
    const out = stagesViewEtaHelper(samples, {
      completed: 12,
      total: 12,
      concurrency: 2,
      previousEtaSec: 8,
    });
    expect(out.etaSec).toBeNull();
    expect(out.perPageSec).toBeNull();
  });
});

// 便利包装：直接驱动内部 eta 计算（与 stagesViewFromPerformance 共享同一实现）。
import { computeEtaFromSamples } from './task-stages';
function stagesViewEtaHelper(
  samples: EtaSample[],
  opts: {
    completed: number;
    total: number;
    concurrency: number;
    previousEtaSec: number | null;
    currentStageLabel?: string | null;
  },
) {
  return computeEtaFromSamples(samples, opts);
}

// ---------------------------------------------------------------------------
// stagesViewFromPerformance：账 → 视图模型（组件直接消费）
// ---------------------------------------------------------------------------
describe('Issue#274 stagesViewFromPerformance — 视图模型', () => {
  it('preflight running ⇒ current=preflight、done 空、无 ETA', () => {
    const view = stagesViewFromPerformance(
      PERF_STAGES([row('preflight', 'running')]),
      { completed: 0, total: 86, previousEtaSec: null },
    );
    expect(view).toEqual({
      current: 'preflight',
      done: [],
      acceleratorRunning: false,
      etaSec: null,
      perPageSec: null,
    });
  });

  it('preflight completed + page_loop running ⇒ done=[preflight]、current=page_loop', () => {
    const view = stagesViewFromPerformance(
      PERF_STAGES([row('preflight', 'completed'), row('page_loop', 'running')]),
      { completed: 5, total: 86, previousEtaSec: null },
    );
    expect(view!.done).toEqual(['preflight']);
    expect(view!.current).toBe('page_loop');
    expect(view!.etaSec).toBeNull(); // 无页样本
  });

  it('page_loop + 足够页样本 ⇒ 带 ETA（缓存页剔除后 p75）', () => {
    const pages: Record<string, unknown> = {};
    const durations = [1000, 2000, 3000, 4000, 1500, 2500, 2200];
    durations.forEach((ms, i) => {
      pages[String(i + 1)] = {
        status: 'completed',
        duration_ms: ms,
        stage_duration_ms: { 视觉识别: ms },
        cache_status: { vision_result: 'hit' },
      };
    });
    const perf = {
      recognition: {
        stages: [row('preflight', 'completed'), row('page_loop', 'running')],
        pages,
        current_segment: { index: 1, total: 1, label: '视觉识别', concurrency: 2 },
      },
    };
    const view = stagesViewFromPerformance(perf, {
      completed: 2,
      total: 12,
      previousEtaSec: null,
    });
    // 全部是缓存命中 ⇒ 有效样本 0 ⇒ 无 ETA（保守）。
    expect(view!.etaSec).toBeNull();
  });

  it('accelerator running 并行 ⇒ current 仍是 page_loop、accelerator 单独标注', () => {
    const view = stagesViewFromPerformance(
      PERF_STAGES([
        row('preflight', 'completed'),
        row('page_loop', 'running'),
        row('accelerator', 'running'),
      ]),
      { completed: 5, total: 86, previousEtaSec: null },
    );
    expect(view!.current).toBe('page_loop');
    expect(view!.acceleratorRunning).toBe(true);
  });
});
