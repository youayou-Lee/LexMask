// Copyright 2026 LexMask Contributors

/** Centralized localStorage / sessionStorage key constants */
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
