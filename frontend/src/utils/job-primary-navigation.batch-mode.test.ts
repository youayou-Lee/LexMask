// Copyright 2026 LexMask Contributors

// Issue#264：导航侧 → 路由白名单契约。resolveBatchRouteMode 的全部可能输出
// （经公共面 resolveJobRowNavigation / buildBatchWorkbenchUrl 观察）必须落在
// 单一来源常量 BATCH_WIZARD_MODES 内——封「导航侧再漂移」：任何人往
// resolveBatchRouteMode 加白名单外的新输出而常量未扩，本契约立即红。
// 反自指判据（AC2）：BATCH_WIZARD_MODES 显式 import 自 constants 模块（跨模块钉），
// 本文件禁止重写字面量数组做断言；job_type 字面量属后端 job_type 域输入词汇
// （backend/app/services/job_models.py CHECK 约束），非模式白名单复刻。
// single 例归属（F4/T12）：resolveJobRowNavigation 的 single 行永不落 /batch——
// 有 single_file_id 落 /single，缺失退 kind:'none'；single→批量向导的 T7 兜底
// 语义只挂 buildBatchWorkbenchUrl 直测段。

import { describe, expect, it } from 'vitest';
import { BATCH_WIZARD_MODES } from '@/constants/batch-wizard-modes';
import {
  buildBatchWorkbenchUrl,
  jobTypeForNav,
  resolveJobRowNavigation,
} from '@/utils/jobPrimaryNavigation';

const BATCH_JOB_TYPES = ['text_batch', 'image_batch', 'smart_batch', 'agent_batch'] as const;

function batchModeOf(to: string): string | null {
  const match = /^\/batch\/([a-z]+)\?/.exec(to);
  return match ? match[1] : null;
}

// batch_wizard_mode 变体：合法值来自常量本身（不重抄），加非法值与缺失两态。
const CONFIG_VARIANTS: Array<{ name: string; batchWizardMode?: string }> = [
  ...BATCH_WIZARD_MODES.map((mode) => ({
    name: `batch_wizard_mode=${mode}`,
    batchWizardMode: mode,
  })),
  { name: 'batch_wizard_mode 非法值', batchWizardMode: 'bogus' },
  { name: 'batch_wizard_mode 缺失' },
];

describe('Issue#264：任务中心批量导航产物必须命中路由白名单', () => {
  describe('resolveJobRowNavigation（任务中心行公共面）', () => {
    it.each(BATCH_JOB_TYPES)(
      '%s：awaiting_review 行全部 config 变体产物 mode ∈ 白名单',
      (jobType) => {
        for (const variant of CONFIG_VARIANTS) {
          const config: Record<string, unknown> = {};
          if (variant.batchWizardMode !== undefined) {
            config.batch_wizard_mode = variant.batchWizardMode;
          }
          const nav = resolveJobRowNavigation({
            id: 'j-1',
            status: 'awaiting_review',
            job_type: jobType,
            config,
            nav_hints: { item_count: 1, first_awaiting_review_item_id: 'i-1' },
          });
          expect(nav.kind, `${jobType} × ${variant.name} 应产出 link`).toBe('link');
          if (nav.kind !== 'link') throw new Error('unreachable');
          const mode = batchModeOf(nav.to);
          expect(mode, `${jobType} × ${variant.name} → ${nav.to}`).not.toBeNull();
          expect(BATCH_WIZARD_MODES, `${jobType} × ${variant.name} 的 mode=${mode}`).toContain(
            mode,
          );
          expect(nav.to).toContain('jobId=j-1');
          expect(nav.to).toContain('step=4');
        }
      },
    );

    it('structured_batch：独占 /structured/delivery 命名空间，不经 /batch', () => {
      const nav = resolveJobRowNavigation({
        id: 'j-1',
        status: 'awaiting_review',
        job_type: 'structured_batch',
        config: {},
      });
      expect(nav.kind).toBe('link');
      if (nav.kind !== 'link') throw new Error('unreachable');
      expect(nav.to.startsWith('/structured/delivery?')).toBe(true);
      expect(nav.to.startsWith('/batch/')).toBe(false);
    });

    it('single 行（T12 分流钉）：有 single_file_id 落 /single，缺失退 none，绝不落 /batch', () => {
      const withFile = resolveJobRowNavigation({
        id: 'j-1',
        status: 'awaiting_review',
        job_type: 'single',
        config: { single_file_id: 'f-1' },
      });
      expect(withFile.kind).toBe('link');
      if (withFile.kind === 'link') {
        expect(withFile.to.startsWith('/single?file_id=f-1')).toBe(true);
        expect(withFile.to.startsWith('/batch/')).toBe(false);
      }
      const withoutFile = resolveJobRowNavigation({
        id: 'j-1',
        status: 'awaiting_review',
        job_type: 'single',
        config: {},
      });
      expect(withoutFile.kind).toBe('none');
    });
  });

  describe('buildBatchWorkbenchUrl（URL 构造直测，含 single T7 兜底）', () => {
    it('single 经 jobTypeForNav 归一后产物 mode ∈ 白名单（T7 兜底口径）', () => {
      const url = buildBatchWorkbenchUrl('j-1', jobTypeForNav('single'), 1, undefined, null);
      const mode = batchModeOf(url);
      expect(mode, url).not.toBeNull();
      expect(BATCH_WIZARD_MODES).toContain(mode);
    });

    it.each(BATCH_JOB_TYPES)('%s：全部 config 变体产物 mode ∈ 白名单且参数原样', (jobType) => {
      for (const variant of CONFIG_VARIANTS) {
        const config: Record<string, unknown> | null =
          variant.batchWizardMode === undefined
            ? null
            : { batch_wizard_mode: variant.batchWizardMode };
        const url = buildBatchWorkbenchUrl('j-1', jobType, 4, 'i-1', config);
        const mode = batchModeOf(url);
        expect(mode, `${jobType} × ${variant.name} → ${url}`).not.toBeNull();
        expect(BATCH_WIZARD_MODES, `${jobType} × ${variant.name} 的 mode=${mode}`).toContain(mode);
        expect(url).toContain('jobId=j-1');
        expect(url).toContain('step=4');
        expect(url).toContain('itemId=i-1');
      }
    });

    it('structured_batch：产物落 /structured/delivery 命名空间', () => {
      const url = buildBatchWorkbenchUrl('j-1', 'structured_batch', 2, undefined, null);
      expect(url.startsWith('/structured/delivery?')).toBe(true);
    });
  });
});
