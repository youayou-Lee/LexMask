// Copyright 2026 LexMask Contributors

/** Centralized localStorage / sessionStorage key constants
 *
 * 破坏性变更（2026-09-28，Issue #1）：`lexmask:*` 前缀取代旧 `datainfraRedaction:*`。
 * 老用户 localStorage 中旧 key 的数据（currentUser、playgroundDraft、vision selection、
 * activePreset）不会自动迁移，相关 UI 状态将重置；AUTH_TOKEN 不带前缀，不受影响。
 * 如需兼容老用户，参照 activePresetBridge.ts 的 LEGACY 桥模式补迁移。
 */
export const STORAGE_KEYS = {
  AUTH_TOKEN: 'auth_token',
  CURRENT_USER: 'lexmask:currentUser',
  LOCALE: 'locale',
  ONBOARDING_COMPLETED: 'onboarding_completed',
  OCR_HAS_TYPES: 'ocrHasTypes',
  VISUAL_FEATURE_TYPES: 'visualFeatureTypes',
  VISION_SELECTION_SIGNATURE: 'lexmask:visionSelectionSignature',
  ACTIVE_PRESET_TEXT_ID: 'lexmask:activePresetTextId',
  ACTIVE_PRESET_TEXT_ID_LEGACY: 'legalRedaction:activePresetTextId',
  ACTIVE_PRESET_VISION_ID: 'lexmask:activePresetVisionId',
  ACTIVE_PRESET_VISION_ID_LEGACY: 'legalRedaction:activePresetVisionId',
  PLAYGROUND_DRAFT: 'lexmask:playgroundDraft',
  BATCH_WIZ_FURTHEST_PREFIX: 'lr_batch_wiz_furthest_',
} as const;
