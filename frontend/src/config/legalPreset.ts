// Copyright 2026 LexMask Contributors

// Issue#262 法律默认档：识别前的唯一显式类型选择 = 「法律卷宗」预设。
//
// 文本侧：11 项法律卷宗高频类型，按下表清单冻结（与 backend/app/core/entity 相关目录
// 的 id 命名一致；LEGAL_* 五项 + PII 四项 + 银行卡 + 案号）。
//
// 视觉侧 v1 零改动：印章/签名本就在后端默认视觉特征集内——backend/config/
// preset_pipeline_types.json 的 visual_features 仅 official_seal、signature 两项
// default_enabled=true（pipeline_service.py VISUAL_FEATURES_DEFAULT_IDS 同口径），
// 沿用后端默认 pipeline 集即可，前端无需 LEGAL_VISUAL_FEATURE_TYPES。

export const LEGAL_TEXT_TYPE_IDS: readonly string[] = [
  'PERSON',
  'ID_CARD',
  'PHONE',
  'ADDRESS',
  'BIRTH_DATE',
  'BANK_CARD',
  'LEGAL_PLAINTIFF',
  'LEGAL_DEFENDANT',
  'LEGAL_THIRD_PARTY',
  'LEGAL_ATTORNEY',
  'CASE_NUMBER',
];

/**
 * 法律集 ∩ 当前目录（按 LEGAL_TEXT_TYPE_IDS 顺序保序）。
 * 交集为空时回落全量 availableIds（fail-open 到后端默认，防目录漂移把识别打空）。
 */
export function resolveLegalTextSelection(availableIds: string[]): string[] {
  const available = new Set(availableIds);
  const legalIds = LEGAL_TEXT_TYPE_IDS.filter((id) => available.has(id));
  return legalIds.length > 0 ? [...legalIds] : [...availableIds];
}
