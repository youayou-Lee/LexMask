// Copyright 2026 LexMask Contributors

import type { BoundingBox, Entity, FileInfo, Stage } from '../types';

/** 草稿结构版本：字段变更时 +1，旧草稿解析即废弃（走重跑路径） */
export const PLAYGROUND_DRAFT_VERSION = 1;
/** 序列化后超过该长度放弃持久化（localStorage 容量保护），会话仅在内存中可用 */
export const PLAYGROUND_DRAFT_MAX_JSON_LENGTH = 2_000_000;

export type DraftSnapshotInput = {
  stage: Stage;
  fileInfo: FileInfo;
  content: string;
  entities: Entity[];
  boundingBoxes: BoundingBox[];
  // 与 use-playground-recognition.ts 中对应 state 的联合类型保持一致（T1 引入 processingMode）
  processingMode: 'mask' | 'replace';
  replacementMode: 'structured' | 'smart' | 'mask' | 'pseudonym';
  watermarkText: string;
  pseudonymMap: Record<string, string>;
  // 用户手改过的映射键：恢复后刷新自动映射时不被覆盖
  pseudonymUserEditedKeys?: string[];
  confirmedPseudonymMap: Record<string, string> | null;
  entityMap: Record<string, string>;
  redactedCount: number;
  currentPage: number;
  // Issue#123（门①.5 r3 N4）：D1 之后识别期 stage 已为 'preview'，中途落盘的草稿
  // 会被 parseDraft 当成完整 preview 恢复（半识别卷被呈现为已完成且不重跑）；
  // 记录流式进度，未完成（completed < total）的草稿一律拒绝 → 走重跑。
  visionStream?: { completed: number; total: number } | null;
  // Issue#194：识别走后台任务后，任务驱动的识别期间落盘的草稿带 job id——
  // 恢复时喂给 SingleTaskProvider.watch 重挂（applyDraftSnapshot）。可选字段、
  // 旧草稿无此值照常解析，故 PLAYGROUND_DRAFT_VERSION 不变。
  taskJobId?: string;
  // Issue#194 终审 C（AC1）：打码在途期间落盘的草稿带打码 job id。收尾守门
  // （use-playground 的 pendingRedact）是纯内存，刷新后不重挂则「任务 completed ⇒
  // 收尾进结果页」的订阅永不触发 ⇒ 成品下载入口（只在结果页）孤儿化。恢复时对称
  // 识别段 reattachTask：重挂 provider 账本，任务已 completed 即自动收尾进结果页。
  pendingRedactJobId?: string;
};

export type PlaygroundDraftSnapshot = DraftSnapshotInput & {
  version: number;
  savedAt: string;
};

export function buildDraftSnapshot(input: DraftSnapshotInput): PlaygroundDraftSnapshot {
  return { version: PLAYGROUND_DRAFT_VERSION, savedAt: new Date().toISOString(), ...input };
}

export function serializeDraft(snapshot: PlaygroundDraftSnapshot): string | null {
  try {
    const json = JSON.stringify(snapshot);
    if (json.length > PLAYGROUND_DRAFT_MAX_JSON_LENGTH) return null;
    return json;
  } catch {
    return null;
  }
}

export function parseDraft(raw: string | null | undefined): PlaygroundDraftSnapshot | null {
  if (!raw) return null;
  let value: PlaygroundDraftSnapshot;
  try {
    value = JSON.parse(raw) as PlaygroundDraftSnapshot;
  } catch {
    return null;
  }
  if (!value || typeof value !== 'object' || value.version !== PLAYGROUND_DRAFT_VERSION) return null;
  if (!value.fileInfo || typeof value.fileInfo.file_id !== 'string') return null;
  if (!Array.isArray(value.entities) || !Array.isArray(value.boundingBoxes)) return null;
  if (typeof value.content !== 'string') return null;
  // Issue#123：识别未完成的半成品不进草稿（宁可重跑，也不给用户一份'看起来完整'的残卷）
  if (value.visionStream && value.visionStream.completed < value.visionStream.total) return null;
  if (value.stage !== 'preview' && value.stage !== 'result') return null;
  // 文本型 preview 却没有正文 → 草稿不完整，宁可重跑也不要白屏
  const isImageMode = value.fileInfo.file_type === 'image' || Boolean(value.fileInfo.is_scanned);
  if (value.stage === 'preview' && !isImageMode && value.content.length === 0) return null;
  return value;
}

export type ResumeDecision =
  | { mode: 'draft'; snapshot: PlaygroundDraftSnapshot }
  | { mode: 'rerun'; fileId: string }
  | { mode: 'unavailable' };

export function planResume(input: { targetFileId: string; snapshot: PlaygroundDraftSnapshot | null }): ResumeDecision {
  const { targetFileId, snapshot } = input;
  if (!targetFileId) return { mode: 'unavailable' };
  if (snapshot && snapshot.fileInfo.file_id === targetFileId) return { mode: 'draft', snapshot };
  return { mode: 'rerun', fileId: targetFileId };
}

export function needsSwitchConfirm(currentFileId: string | null, targetFileId: string): boolean {
  return Boolean(currentFileId) && currentFileId !== targetFileId;
}

// ---------------------------------------------------------------------------
// 服务端识别缓存（R2 秒回）：后端每次 NER 成功都会把 entities + 当时的识别配置
// 写进文件记录（GET /files/{id} 可取）。命中则直接恢复「当时的现场」，
// 未识别过（实体为空，含扫描件恒空）才重跑。
// ---------------------------------------------------------------------------

export type ServerResumeInfo = {
  entities?: unknown;
  recognition_config?: { entity_type_ids?: string[] | null } | null;
};

export type ServerCachedResumeDecision =
  | { mode: 'cached'; entities: Record<string, unknown>[]; entityTypeIds: string[] | null }
  | { mode: 'rerun' };

export function planServerCachedResume(info: ServerResumeInfo): ServerCachedResumeDecision {
  if (!Array.isArray(info.entities) || info.entities.length === 0) return { mode: 'rerun' };
  const ids = info.recognition_config?.entity_type_ids;
  return {
    mode: 'cached',
    entities: info.entities as Record<string, unknown>[],
    entityTypeIds: Array.isArray(ids) ? ids : null,
  };
}

/** 超长单页文本的虚拟分页：按固定字符窗口切分，避免全文+全实体一次性渲染 */
export function splitVirtualPages(content: string, limit: number): string[] {
  if (limit <= 0 || content.length <= limit) return [content];
  const chunks: string[] = [];
  for (let i = 0; i < content.length; i += limit) {
    chunks.push(content.slice(i, i + limit));
  }
  return chunks;
}
