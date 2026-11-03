// Copyright 2026 LexMask Contributors

// Issue#194 用户验收③：视觉管线两段式（先 OCR 一遍 N 页，再视觉要素一遍 N 页），
// 进度数字两轮 0→N 相同，只显示数字时用户把第二段看成「重新开始」。
// 钉住：服务端 progress_message（task_queue_pipelines.py 真实取值）→ 人话段名 i18n 键
// + 页码；未知/无段名消息一律 null（调用方回落既有「后台识别中 current/total」文案）。

import { describe, expect, it } from 'vitest';

import {
  currentSegmentFromPerformance,
  inFlightPages,
  recognitionStageFromMessage,
  stageStartedAtFromPerformance,
} from './task-progress';

describe('recognitionStageFromMessage — 后端 message → 人话段名', () => {
  it('两段式视觉管线的逐页消息：段名键 + 页码（stage_label 实际取值逐个钉）', () => {
    // task_queue_pipelines.py:488 run_page_stage stage_label="OCR+HaS识别"
    expect(recognitionStageFromMessage('OCR+HaS识别 page 3/20')).toEqual({
      labelKey: 'playground.taskStage.ocrHas',
      current: 3,
      total: 20,
    });
    // task_queue_pipelines.py:497 merge pass stage_label="视觉特征识别"
    expect(recognitionStageFromMessage('视觉特征识别 page 5/20')).toEqual({
      labelKey: 'playground.taskStage.visualFeatures',
      current: 5,
      total: 20,
    });
    // run_page 默认形参 stage_label="视觉识别"
    expect(recognitionStageFromMessage('视觉识别 page 1/2')).toEqual({
      labelKey: 'playground.taskStage.vision',
      current: 1,
      total: 2,
    });
  });

  it('文本件 NER 段（stage=ner current=total=1）→ 实体识别', () => {
    expect(recognitionStageFromMessage('text_recognition_running')).toEqual({
      labelKey: 'playground.taskStage.text',
      current: 1,
      total: 1,
    });
  });

  it('无段名的消息（排队/完成/解析）→ null（调用方回落既有文案）', () => {
    expect(recognitionStageFromMessage('Vision recognition queued for 20 page(s)')).toBeNull();
    expect(recognitionStageFromMessage('vision_recognition_complete')).toBeNull();
    expect(recognitionStageFromMessage('text_recognition_complete')).toBeNull();
  });

  it('空/缺失/意外形状 → null', () => {
    expect(recognitionStageFromMessage(null)).toBeNull();
    expect(recognitionStageFromMessage(undefined)).toBeNull();
    expect(recognitionStageFromMessage('')).toBeNull();
    expect(recognitionStageFromMessage('OCR+HaS识别 page 3')).toBeNull();
    expect(recognitionStageFromMessage('识别 page 3/20')).toBeNull();
    // 前后缀注入不匹配（整串匹配，防误映射）
    expect(recognitionStageFromMessage('prefix OCR+HaS识别 page 3/20')).toBeNull();
  });
});


// --- Issue#238：打码段消息映射 + 诚实化取数纯函数 ---

describe('Issue#238 recognitionStageFromMessage — 打码逐页消息', () => {
  it('打码 page p/N → redaction 段名键 + 页码', () => {
    expect(recognitionStageFromMessage('打码 page 92/149')).toEqual({
      labelKey: 'playground.taskStage.redaction',
      current: 92,
      total: 149,
    });
  });
  it('前缀注入不误映射', () => {
    expect(recognitionStageFromMessage('evil 打码 page 1/1')).toBeNull();
    expect(recognitionStageFromMessage('打码 page 1/1 extra')).toBeNull();
  });
});

describe('Issue#238 currentSegmentFromPerformance — current_segment 取数', () => {
  it('合法段信息：index/total/concurrency 透传', () => {
    expect(
      currentSegmentFromPerformance({
        recognition: { current_segment: { index: 2, total: 2, label: '视觉特征识别', concurrency: 1 } },
      }),
    ).toEqual({ index: 2, total: 2, concurrency: 1 });
  });
  it('concurrency 缺失/非法 → null（不显示并行中）', () => {
    expect(
      currentSegmentFromPerformance({ recognition: { current_segment: { index: 1, total: 1 } } }),
    ).toEqual({ index: 1, total: 1, concurrency: null });
    expect(
      currentSegmentFromPerformance({
        recognition: { current_segment: { index: 1, total: 1, concurrency: -1 } },
      }),
    ).toEqual({ index: 1, total: 1, concurrency: null });
  });
  it('缺键/坏形 → null（旧任务回落不误报）', () => {
    expect(currentSegmentFromPerformance(null)).toBeNull();
    expect(currentSegmentFromPerformance({})).toBeNull();
    expect(
      currentSegmentFromPerformance({ recognition: { current_segment: { index: 0, total: 2 } } }),
    ).toBeNull();
  });
});

describe('Issue#238 inFlightPages — 并行处理中页数', () => {
  it('min(段并发, 剩余页)；串行段（concurrency<=1）与缺段 → null', () => {
    const seg = { index: 1, total: 2, concurrency: 4 };
    expect(inFlightPages(seg, 84, 149)).toBe(4);
    expect(inFlightPages(seg, 147, 149)).toBe(2);
    expect(inFlightPages(seg, 149, 149)).toBe(0);
    expect(inFlightPages({ index: 1, total: 1, concurrency: 1 }, 5, 20)).toBeNull();
    expect(inFlightPages(null, 5, 20)).toBeNull();
    expect(inFlightPages({ index: 1, total: 2, concurrency: null }, 5, 20)).toBeNull();
  });
});

describe('Issue#238 stageStartedAtFromPerformance — 已运行时长起算点', () => {
  it('recognition/redaction 两段分别取 started_at；缺键 → null', () => {
    const perf = {
      recognition: { started_at: '2026-01-01T00:00:00Z' },
      redaction: { started_at: '2026-01-01T00:10:00Z' },
    };
    expect(stageStartedAtFromPerformance(perf, 'recognition')).toBe('2026-01-01T00:00:00Z');
    expect(stageStartedAtFromPerformance(perf, 'redaction')).toBe('2026-01-01T00:10:00Z');
    expect(stageStartedAtFromPerformance(null, 'redaction')).toBeNull();
    expect(stageStartedAtFromPerformance({}, 'recognition')).toBeNull();
  });
});
