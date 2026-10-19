// Copyright 2026 LexMask Contributors

// Issue#194 Task 11：处理详情行构建纯函数（spec §5 映射表逐字钉）。
//
// fixture 键形逐键对照真实工人写入：
// - backend/tests/test_single_task_list_contract.py（T6 契约：别名键
//   queue_wait_ms / recognition_duration_ms / recognition_pages / redaction_duration_ms，
//   页记录 page/duration_ms/status，status 成功值为 "completed"）；
// - backend/app/services/task_queue_pipelines.py（recognition.parse_ms / ner_ms /
//   mode / page_count / pages 按字符串页号键控；redaction.duration_ms/entity_count/
//   bounding_box_count）；
// - backend/app/services/task_queue_metrics.py::_duration_breakdown_from_quality
//   （duration_ms 原样 + pipeline_status.*.stage_duration_ms 拍平为 "前缀.键"：
//   ocr_has.ocr / ocr_has.has_ner / ocr_has.match；pdf_render_ms 在顶层；
//   visual_features 为页级管线总时长键）。
//
// 钉住的不变量：
// 1) 行序与键名按 spec §5：queue_wait / parse / ocr / match / ner /
//    visual_features / redaction / failed_chunks；
// 2) 空值族：全 null → []（旧文件条款）；0 页 / 0 实体 / 缺 pages / 缺 redaction 各自不渲染对应行；
// 3) 文本件（mode==='text' 或无 pages 键）无 OCR 行；
// 4) 失败回落行只数非成功状态页（completed 为成功；failed/timeout 计入），0 缺失不显示；
// 5) 别名形状（API 顶层别名键）与原始 performance 形状产出逐项一致的行；
// 6) 总耗时 = recognition.duration_ms + redaction.duration_ms，排队不并入。

import { describe, expect, it } from 'vitest';

import {
  buildProcessingRows,
  buildProcessingTotalMs,
  formatDurationMs,
  isTextDirectMode,
  mergeFilePageCount,
} from './processing-details';

// —— 完整视觉件 fixture：三页（两成一败）、breakdown 各键齐全 ——
const FULL_VISION_PERF = {
  queue: { last_task_type: 'recognition', last_wait_ms: 120 },
  recognition: {
    mode: 'vision',
    page_count: 3,
    queue_wait_ms: 120,
    parse_ms: 500,
    duration_ms: 4500,
    pages: {
      '1': {
        page: 1,
        duration_ms: 1500,
        status: 'completed',
        duration_breakdown_ms: {
          ocr_has: 1400,
          visual_features: 300,
          pdf_render_ms: 250,
          total: 1500,
          'ocr_has.prepare': 100,
          'ocr_has.ocr': 900,
          'ocr_has.has_ner': 200,
          'ocr_has.match': 80,
          'ocr_has.draw': 30,
        },
      },
      '2': {
        page: 2,
        duration_ms: 1200,
        status: 'completed',
        duration_breakdown_ms: {
          ocr_has: 1100,
          pdf_render_ms: 150,
          'ocr_has.ocr': 800,
          'ocr_has.has_ner': 150,
          'ocr_has.match': 60,
        },
      },
      '3': {
        page: 3,
        duration_ms: 900,
        status: 'failed',
        duration_breakdown_ms: { pdf_render_ms: 100 },
      },
    },
  },
  redaction: { finished_at: '2026-10-18T00:00:03+00:00', duration_ms: 880, entity_count: 12, bounding_box_count: 34 },
};

describe('buildProcessingRows — spec §5 完整映射', () => {
  const rows = buildProcessingRows(FULL_VISION_PERF);

  it('行序与键名按 spec §5 逐项', () => {
    expect(rows.map((row) => row.key)).toEqual([
      'queue_wait',
      'parse',
      'ocr',
      'match',
      'ner',
      'visual_features',
      'redaction',
      'failed_chunks',
    ]);
  });

  it('每行 labelKey / durationMs 逐项', () => {
    expect(rows.map((row) => [row.key, row.labelKey, row.durationMs])).toEqual([
      ['queue_wait', 'processingDetails.queueWait', 120],
      ['parse', 'processingDetails.parse', 500],
      ['ocr', 'processingDetails.ocr', 2200], // Σ ocr_has.ocr (900+800) + Σ pdf_render_ms (250+150+100)
      ['match', 'processingDetails.match', 140], // Σ ocr_has.match (80+60)
      ['ner', 'processingDetails.ner', 350], // Σ ocr_has.has_ner (200+150)，recognition.ner_ms 缺失
      ['visual_features', 'processingDetails.visualFeatures', 300], // Σ 页级 visual_features
      ['redaction', 'processingDetails.redaction', 880],
      ['failed_chunks', 'processingDetails.failedChunks', null],
    ]);
  });

  it('counts 逐项（解析页数 / OCR 页数+均速 / 打码实体+框 / 失败回落 warn）', () => {
    const byKey = Object.fromEntries(rows.map((row) => [row.key, row]));
    expect(byKey.queue_wait.counts).toBeNull();
    expect(byKey.parse.counts).toBe('3 页');
    expect(byKey.ocr.counts).toBe('3 页 · 均 不足 1 秒/页'); // 2200/3 = 733.33 → 733 → 不足 1 秒
    expect(byKey.match.counts).toBeNull();
    expect(byKey.ner.counts).toBeNull();
    expect(byKey.visual_features.counts).toBeNull();
    expect(byKey.redaction.counts).toBe('12 个实体 · 34 个框');
    expect(byKey.failed_chunks.counts).toBe('有 1 处识别失败，已自动回落');
    expect(byKey.failed_chunks.tone).toBe('warn');
    expect(byKey.queue_wait.tone).toBeUndefined();
  });

  it('总耗时 = 识别段 + 打码段，排队等待不并入', () => {
    expect(buildProcessingTotalMs(FULL_VISION_PERF)).toBe(5380); // 4500 + 880（120 不并入）
  });

  it('排队等待三级来源：顶层别名 > recognition.queue_wait_ms > queue.last_wait_ms', () => {
    expect(buildProcessingRows({ queue: { last_wait_ms: 7 } })).toEqual([
      { key: 'queue_wait', labelKey: 'processingDetails.queueWait', durationMs: 7, counts: null },
    ]);
    expect(
      buildProcessingRows({
        queue: { last_wait_ms: 7 },
        recognition: { queue_wait_ms: 9 },
      })[0].durationMs,
    ).toBe(9);
    expect(buildProcessingRows({ queue_wait_ms: 11, queue: { last_wait_ms: 7 } })[0].durationMs).toBe(11);
  });

  it('ner 行优先取 recognition.ner_ms，缺失才 Σ 页级 has_ner', () => {
    const withNerMs = buildProcessingRows({
      recognition: { ner_ms: 70, pages: FULL_VISION_PERF.recognition.pages },
    });
    expect(withNerMs.find((row) => row.key === 'ner')?.durationMs).toBe(70);
  });
});

describe('buildProcessingRows — 别名形状与原始形状产出一致（T6 契约双形状）', () => {
  it('API 顶层别名键 + performance 嵌套键 → 行与原始 performance 形状逐项相等', () => {
    const aliasPerf = {
      queue_wait_ms: 120,
      recognition_duration_ms: 4500,
      recognition_pages: [
        FULL_VISION_PERF.recognition.pages['1'],
        FULL_VISION_PERF.recognition.pages['2'],
        FULL_VISION_PERF.recognition.pages['3'],
      ],
      redaction_duration_ms: 880,
      redaction: { entity_count: 12, bounding_box_count: 34 },
      recognition: { mode: 'vision', page_count: 3, parse_ms: 500 },
    };
    expect(buildProcessingRows(aliasPerf)).toEqual(buildProcessingRows(FULL_VISION_PERF));
    expect(buildProcessingTotalMs(aliasPerf)).toBe(buildProcessingTotalMs(FULL_VISION_PERF));
  });

  it('recognition_pages 为数组（API 别名形状）与字符串页号记录（工人形状）等价', () => {
    const fromArray = buildProcessingRows({
      recognition: { page_count: 2, pages: [FULL_VISION_PERF.recognition.pages['1'], FULL_VISION_PERF.recognition.pages['2']] },
    });
    const fromRecord = buildProcessingRows({
      recognition: { page_count: 2, pages: { '1': FULL_VISION_PERF.recognition.pages['1'], '2': FULL_VISION_PERF.recognition.pages['2'] } },
    });
    expect(fromArray).toEqual(fromRecord);
  });
});

describe('buildProcessingRows — 空值族（AC3 旧文件条款）', () => {
  it('null / undefined / 空对象 → []（组件据此不渲染卡）', () => {
    expect(buildProcessingRows(null)).toEqual([]);
    expect(buildProcessingRows(undefined)).toEqual([]);
    expect(buildProcessingRows({})).toEqual([]);
    expect(buildProcessingRows({ recognition: {} })).toEqual([]);
  });

  it('0 页（pages 空表）→ 无 OCR 行', () => {
    const rows = buildProcessingRows({ recognition: { mode: 'vision', page_count: 0, pages: {} } });
    expect(rows).toEqual([]);
  });

  it('0 实体 0 框 → 打码行仍显示时长，counts 为 null', () => {
    const rows = buildProcessingRows({ redaction: { duration_ms: 50, entity_count: 0, bounding_box_count: 0 } });
    expect(rows).toHaveLength(1);
    expect(rows[0]).toMatchObject({ key: 'redaction', durationMs: 50, counts: null });
  });

  it('缺 pages → 无 OCR / match / visual_features 行；缺 redaction → 无打码行', () => {
    const rows = buildProcessingRows({ recognition: { mode: 'vision', parse_ms: 10, duration_ms: 99 } });
    expect(rows.map((row) => row.key)).toEqual(['parse']);
    expect(buildProcessingTotalMs({ recognition: { duration_ms: 99 } })).toBe(99);
    expect(buildProcessingTotalMs({})).toBeNull();
  });

  it('全页 completed → 无失败回落行；timeout 计入失败；status 缺失不计入', () => {
    const allCompleted = buildProcessingRows({
      recognition: {
        pages: {
          '1': { page: 1, duration_ms: 10, status: 'completed', duration_breakdown_ms: {} },
          '2': { page: 2, duration_ms: 10, status: 'completed', duration_breakdown_ms: {} },
        },
      },
    });
    expect(allCompleted.find((row) => row.key === 'failed_chunks')).toBeUndefined();

    const withTimeout = buildProcessingRows({
      recognition: {
        pages: {
          '1': { page: 1, duration_ms: 10, status: 'completed', duration_breakdown_ms: {} },
          '2': { page: 2, duration_ms: 10, status: 'timeout' },
          '3': { page: 3, duration_ms: 10 },
        },
      },
    });
    const failedRow = withTimeout.find((row) => row.key === 'failed_chunks');
    expect(failedRow).toMatchObject({ durationMs: null, counts: '有 1 处识别失败，已自动回落', tone: 'warn' });
  });

  it('文本件（mode=text 或无 pages 键）无 OCR 行；textDirect 判定', () => {
    const textRows = buildProcessingRows({
      recognition: { mode: 'text', parse_ms: 20, ner_ms: 40, page_count: 1 },
    });
    expect(textRows.map((row) => row.key)).toEqual(['parse', 'ner']);
    expect(isTextDirectMode({ recognition: { mode: 'text' } })).toBe(true);
    expect(isTextDirectMode(FULL_VISION_PERF)).toBe(false);
    expect(isTextDirectMode({})).toBe(false);
  });

  it('OCR 行存在但 breakdown 缺失 → 行保留、值为 0；match/visual_features 无键则整行不显示', () => {
    const rows = buildProcessingRows({
      recognition: {
        mode: 'vision',
        pages: {
          '1': { page: 1, duration_ms: 10, status: 'completed' },
        },
      },
    });
    expect(rows.map((row) => row.key)).toEqual(['ocr']);
    expect(rows[0].durationMs).toBe(0);
    // 页记录存在（分母 1）→ 均速段保留，均速如实为 0；「分母 0 兜底」只在无页记录时成立，
    // 而无页记录整行都不显示（上一用例），故兜底文案不出现于任何可达路径。
    expect(rows[0].counts).toBe('1 页 · 均 不足 1 秒/页');
  });
});

describe('formatDurationMs / mergeFilePageCount', () => {
  it('formatDurationMs：中文三档（不足 1 秒 / X 秒 / X 分 Y 秒，Issue#194 用户验收②）', () => {
    // <1s → 不足 1 秒
    expect(formatDurationMs(0)).toBe('不足 1 秒');
    expect(formatDurationMs(880)).toBe('不足 1 秒');
    expect(formatDurationMs(999)).toBe('不足 1 秒');
    // <60s → 整数秒
    expect(formatDurationMs(1000)).toBe('1 秒');
    expect(formatDurationMs(6000)).toBe('6 秒');
    // ≥60s → 分 + 秒（60.2s 属此档，不再出现「60.2s」英文单位裸数字）
    expect(formatDurationMs(61_000)).toBe('1 分 1 秒');
    expect(formatDurationMs(130_000)).toBe('2 分 10 秒');
    expect(formatDurationMs(60_200)).toBe('1 分 0 秒');
    // 进位边界：59999ms 四舍五入 60 秒 → 1 分 0 秒
    expect(formatDurationMs(59_999)).toBe('1 分 0 秒');
    expect(formatDurationMs(59_950)).toBe('1 分 0 秒');
  });

  it('mergeFilePageCount：文本件 page_count 组件层合并，已有值不覆盖，非法输入原样返回', () => {
    const perf = { recognition: { parse_ms: 5 } };
    const merged = mergeFilePageCount(perf, 7)!;
    expect(merged.recognition).toMatchObject({ parse_ms: 5, page_count: 7 });
    // 已有 page_count 优先（视觉件后端已写）
    expect(mergeFilePageCount(FULL_VISION_PERF, 99)!.recognition).toMatchObject({ page_count: 3 });
    // 无补充值 → 原对象
    expect(mergeFilePageCount(perf, null)).toBe(perf);
    expect(mergeFilePageCount(null, 3)).toBeNull();
  });
});
