// Copyright 2026 LexMask Contributors

// Issue#194 用户验收③：视觉管线两段式（先 OCR 一遍 N 页，再视觉要素一遍 N 页），
// 进度数字两轮 0→N 相同，只显示数字时用户把第二段看成「重新开始」。
// 钉住：服务端 progress_message（task_queue_pipelines.py 真实取值）→ 人话段名 i18n 键
// + 页码；未知/无段名消息一律 null（调用方回落既有「后台识别中 current/total」文案）。

import { describe, expect, it } from 'vitest';

import { recognitionStageFromMessage } from './task-progress';

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
