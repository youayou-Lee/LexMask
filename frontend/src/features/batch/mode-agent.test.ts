// Copyright 2026 LexMask Contributors

// Issue#196 供料模式批量：模式工具改名判据 + i18n 旧词清零（CI 接线，可删即红）。

import { describe, expect, it } from 'vitest';

import {
  isBatchFileAllowedForMode,
} from './utils/file-type';
import {
  buildJobConfigForWorker,
  isBatchWizardMode,
  toBatchJobType,
} from './hooks/use-batch-wizard-utils';
import { batchWizardStorageKey } from '@/services/batchPipeline';
import { zh } from '@/i18n/zh';
import { en } from '@/i18n/en';
import type { BatchWizardPersistedConfig } from '@/services/batchPipeline';
import type { FileType } from '@/types';

describe('供料模式（agent 通道）模式工具', () => {
  it('isBatchWizardMode 接受 agent', () => {
    expect(isBatchWizardMode('agent')).toBe(true);
    expect(isBatchWizardMode('Agent')).toBe(false);
    expect(isBatchWizardMode('feed')).toBe(false);
  });

  it('toBatchJobType：agent → agent_batch，既有三通道不受影响', () => {
    expect(toBatchJobType('agent')).toBe('agent_batch');
    expect(toBatchJobType('text')).toBe('text_batch');
    expect(toBatchJobType('image')).toBe('image_batch');
    expect(toBatchJobType('smart')).toBe('smart_batch');
  });

  it('供料通道全格式放行（D3：与 smart 同口径）', () => {
    const all: FileType[] = [
      'docx' as FileType,
      'doc' as FileType,
      'txt' as FileType,
      'pdf' as FileType,
      'pdf_scanned' as FileType,
      'image' as FileType,
    ];
    for (const ft of all) {
      expect(isBatchFileAllowedForMode('agent', ft)).toBe(true);
    }
  });

  it('供料 job config：replacement_mode 锁定 placeholder，不带图像打码/水印', () => {
    const cfg = {
      selectedEntityTypeIds: ['PERSON'],
      ocrHasTypes: ['id_number'],
      visualFeatureTypes: ['seal'],
      replacementMode: 'structured' as BatchWizardPersistedConfig['replacementMode'],
      imageRedactionMethod: 'mosaic' as const,
      imageRedactionStrength: 75,
      imageFillColor: '#000000',
      watermarkText: '水印',
      presetTextId: null,
      presetVisionId: null,
      executionDefault: 'queue' as const,
    };
    const out = buildJobConfigForWorker(cfg, 'agent', 1);
    expect(out['replacement_mode']).toBe('placeholder');
    expect(out['entity_type_ids']).toEqual(['PERSON']);
    expect(out['batch_wizard_mode']).toBe('agent');
    expect(out).not.toHaveProperty('image_redaction_method');
    expect(out).not.toHaveProperty('watermark_text');
    expect(out).not.toHaveProperty('ocr_has_types');
    // 普通通道行为不变
    const normal = buildJobConfigForWorker(cfg, 'text', 1);
    expect(normal['replacement_mode']).toBe('structured');
    expect(normal).toHaveProperty('watermark_text');
  });

  it('agent 模式使用独立草稿存储键', () => {
    expect(batchWizardStorageKey('agent')).toBe('batchWizard:config:v3:agent');
    expect(batchWizardStorageKey('smart')).toBe('batchWizard:config:v3:smart');
  });
});

describe('供料模式改名（Issue#196 D1）：用户可见文案旧词清零', () => {
  const collectValues = (table: Record<string, string>): string[] => Object.values(table);

  it('zh 不再出现旧词「喂Agent / 喂 Agent」（代码注释与 git 历史除外）', () => {
    const offenders = collectValues(zh).filter((v) => /喂 ?Agent/.test(v));
    expect(offenders).toEqual([]);
  });

  it('en 不再出现旧词「Feed Agent」', () => {
    const offenders = collectValues(en).filter((v) => /Feed Agent/i.test(v));
    expect(offenders).toEqual([]);
  });

  it('新词在位：nav.agentMd 双语均为供料模式', () => {
    expect(zh['nav.agentMd']).toBe('供料模式');
    expect(zh['agentMd.title']).toContain('供料模式');
    expect(zh['playground.feedAgentHint']).toContain('供料模式');
    expect(en['nav.agentMd']).toBe('AI feed mode');
    expect(en['agentMd.title']).toContain('AI feed mode');
  });

  it('供料通道 hub/向导文案键双语齐备（i18n parity 之外的内容在位断言）', () => {
    for (const key of [
      'batchHub.mode.agent.title',
      'batchHub.mode.agent.desc',
      'batchHub.mode.agent.tag1',
      'batchHub.mode.agent.summaryValue',
      'batchHub.mode.agent.journeyNote',
      'batchWizard.step1Agent.modeLabel',
      'batchWizard.step1Agent.placeholderNote',
      'batchWizard.step4Agent.hint',
      'batchWizard.step4Agent.confirmGenerate',
    ] as const) {
      expect(zh[key], `zh missing ${key}`).toBeTruthy();
      expect(en[key], `en missing ${key}`).toBeTruthy();
    }
  });
});

// ---------------------------------------------------------------------------
// 向导任务恢复白名单（浏览器验收发现的缺口）：任务中心「继续审阅」深链对
// agent_batch 必须可用——use-batch-wizard 的 hydrate 白名单此前漏了
// agent_batch，恢复时误报「当前任务类型与这个流程不匹配」并退回 step1。
// ---------------------------------------------------------------------------

describe('供料批向导恢复（hydrate 白名单）', () => {
  it('isHydratableBatchJobType：agent_batch 可恢复，四通道齐、非批拒', async () => {
    const { isHydratableBatchJobType } = await import('./hooks/use-batch-wizard-utils');
    expect(isHydratableBatchJobType('agent_batch')).toBe(true);
    expect(isHydratableBatchJobType('smart_batch')).toBe(true);
    expect(isHydratableBatchJobType('text_batch')).toBe(true);
    expect(isHydratableBatchJobType('image_batch')).toBe(true);
    expect(isHydratableBatchJobType('single')).toBe(false);
    expect(isHydratableBatchJobType('structured_batch')).toBe(false);
    expect(isHydratableBatchJobType('')).toBe(false);
  });

  it('jobTypeNavForHydrate：agent_batch 原样透传，不回落 smart_batch', async () => {
    const { jobTypeNavForHydrate } = await import('./hooks/use-batch-wizard-utils');
    expect(jobTypeNavForHydrate('agent_batch')).toBe('agent_batch');
    expect(jobTypeNavForHydrate('smart_batch')).toBe('smart_batch');
    expect(jobTypeNavForHydrate('single')).toBe('smart_batch');
  });
});

describe('供料批向导恢复（hydrate 行装载带 item_id）', () => {
  // 缺口同源：use-batch-wizard hydrate 重建的 BatchRow 未带 item_id，刷新后
  // step4 的 reviewableAgentRows 按 Boolean(row.item_id) 过滤 → 供料审阅页空。
  // 正常流程行来自文件列表 API（自带 item_id），所以「初进可用、刷新必坏」。
  const ITEMS = [
    {
      id: 'item-1',
      file_id: 'file-1',
      filename: 'a.pdf',
      file_type: 'pdf',
      status: 'awaiting_review',
      has_output: false,
      entity_count: 8,
      created_at: '2026-11-03T01:00:00',
      has_review_draft: false,
      error_message: null,
    },
    {
      id: 'item-2',
      file_id: 'file-2',
      filename: 'b.txt',
      file_type: 'txt',
      status: 'completed',
      has_output: true,
      entity_count: 3,
      created_at: '2026-11-03T01:01:00',
      has_review_draft: true,
      error_message: null,
    },
  ];

  it('buildRowsFromJobDetailItems：行带 item_id 且状态映射正确', async () => {
    const { buildRowsFromJobDetailItems } = await import('./hooks/use-batch-wizard-utils');
    const rows = buildRowsFromJobDetailItems(ITEMS, { analyzeErrorFallback: '失败' });
    expect(rows.map((r) => r.item_id)).toEqual(['item-1', 'item-2']);
    expect(rows.map((r) => r.analyzeStatus)).toEqual(['awaiting_review', 'completed']);
    expect(rows[0].file_id).toBe('file-1');
    expect(rows[1].reviewConfirmed).toBe(true);
  });

  it('失败行带错误消息，未失败行不带', async () => {
    const { buildRowsFromJobDetailItems } = await import('./hooks/use-batch-wizard-utils');
    const rows = buildRowsFromJobDetailItems(
      ITEMS.map((i) => ({ ...i, status: 'failed', error_message: 'boom' })),
      { analyzeErrorFallback: '失败' },
    );
    expect(rows[0].analyzeStatus).toBe('failed');
    expect(rows[0].analyzeError).toBe('boom');
  });
});
