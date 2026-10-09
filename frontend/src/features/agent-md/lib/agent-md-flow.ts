// Copyright 2026 LexMask Contributors

/** /agent-md 页面的流程步骤（Issue#75 喂 Agent 并行模式）。 */
export type FlowStep = 'upload' | 'processing' | 'review' | 'result';

/** GET /agent-md/{id}/mapping 返回的单条映射项（后端 snake_case 原样）。 */
export interface MappingItemDto {
  id: string;
  original_text: string;
  entity_type: string;
  replacement: string;
  excluded: boolean;
}

/** POST /agent-md/{id}/confirm 的单条决策。 */
export interface Decision {
  id: string;
  action: 'keep' | 'exclude' | 'custom';
  replacement?: string;
}

/** 页面本地维护的逐条编辑（只记用户改过的项）。 */
export type ItemEdits = Record<string, { action: Decision['action']; replacement?: string }>;

/** 后端状态机字面量 → 前端流程步骤：就绪进评审、完成进出稿、其余（含 failed）留在处理中展示失败。 */
export function nextStepFromStatus(state: string): FlowStep {
  switch (state) {
    case 'mapping_ready':
      return 'review';
    case 'completed':
      return 'result';
    default:
      return 'processing';
  }
}

/**
 * 只为用户改过的项生成决策：按映射项顺序输出（而非 edits 插入序），
 * 并丢弃映射表里已不存在的陈旧编辑 id（映射刷新后的残留）。
 */
export function buildDecisions(items: MappingItemDto[], edits: ItemEdits): Decision[] {
  return items
    .filter((item) => edits[item.id] !== undefined)
    .map((item) => {
      const edit = edits[item.id];
      return {
        id: item.id,
        action: edit.action,
        ...(edit.replacement !== undefined ? { replacement: edit.replacement } : {}),
      };
    });
}
