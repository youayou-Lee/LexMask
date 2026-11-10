// Copyright 2026 LexMask Contributors

// batch 向导 step3 的识别 progress message → i18n 键映射（纯函数，供组件与
// 契约测试共用；Issue#256 起包含视觉段跳过终态）。未知消息返回 null，调用方
// 回落显示原文——与 playground/lib/task-progress.ts 的映射纪律一致。
export function batchRecognitionMessageKey(rawMessage: string | null | undefined): string | null {
  if (!rawMessage) return null;
  switch (rawMessage.trim()) {
    case 'text_recognition_running':
    case 'Text recognition running':
      return 'batchWizard.step3.fileTextProgress';
    case 'text_recognition_complete':
    case 'Text recognition complete':
      return 'batchWizard.step3.fileTextComplete';
    case 'vision_recognition_complete':
    case 'Vision recognition complete':
      return 'batchWizard.step3.fileVisionComplete';
    // Issue#256：LA 不可用时视觉段被跳过的识别终态——照常完成但印章/签字未识别，
    // 显式提示不静默（PR#235 同语义）。
    case 'vision_recognition_complete_visual_skipped':
      return 'batchWizard.step3.fileVisionCompleteSkipped';
    case 'Requeued after stale processing heartbeat':
      return 'batchWizard.step3.requeuedAfterStale';
    default:
      return null;
  }
}
