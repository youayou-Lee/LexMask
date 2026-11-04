// Copyright 2026 LexMask Contributors

// Issue#244 任务 B：上传弹窗的水印检测预告。判定在 lib/watermark-tool 纯函数，
// 这里只负责把判定结果发成既有 toast（duration 拉长到 6s 让用户读完）。
// 调用方约定：请求失败/静默 code 一律不发，弹窗永不阻断上传流程。

import { showToast } from '@/components/Toast';
import { t } from '@/i18n';
import { watermarkUploadToastPlan } from '@/lib/watermark-tool';

export function notifyWatermarkUploadStatus(status: unknown): void {
  const plan = watermarkUploadToastPlan(status);
  if (!plan.notice || !plan.messageKey) return;
  const message =
    plan.pages !== null ? t(plan.messageKey).replace('{pages}', String(plan.pages)) : t(plan.messageKey);
  showToast(message, 'info', { duration: 6000 });
}
