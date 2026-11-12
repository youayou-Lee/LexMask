// Copyright 2026 LexMask Contributors

// Issue#264：批量向导模式白名单单一来源。类型由常量派生（非并列声明），
// 收窄/扩圈只动本文件一处；路由放行（router.tsx BatchRoute）、导航解析
// （utils/jobPrimaryNavigation resolveBatchRouteMode）、wizard 模式守卫
// （features/batch/hooks/use-batch-wizard-utils）三处历史重复白名单全部
// 收敛到这里。零依赖叶子模块——路由层不得引入 services 依赖图。
export const BATCH_WIZARD_MODES = ['text', 'image', 'smart', 'agent'] as const;

export type BatchWizardMode = (typeof BATCH_WIZARD_MODES)[number];

export function isBatchWizardMode(value: unknown): value is BatchWizardMode {
  return (BATCH_WIZARD_MODES as readonly string[]).includes(value as string);
}
