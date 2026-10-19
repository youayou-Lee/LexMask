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
