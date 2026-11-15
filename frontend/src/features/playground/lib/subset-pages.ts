// Copyright 2026 LexMask Contributors

import type { ItemPageStatus } from '@/services/jobsApi';

export interface SubsetSelection {
  /** 子集页清单（升序、去重；0/负页码已滤除）。 */
  pages: number[];
  /** 原卷总页数（页账 page_count，退化到 file_info.page_count）。 */
  total: number;
}

/**
 * Issue#327 子集预览的入口判定 + 子集计算（纯函数，便于单测）。
 *
 * 门禁（门⑥ I-1）：只有**识别仍在跑**才允许子集口径。识别已终态（awaiting_review/
 * completed/failed/cancelled）时页账不再增长，必须走既有全卷链路——失败页由 Issue#199
 * 的显式失败记账 + 知情确认覆盖，静默降级成「只处理前 N 页」= fail-open。
 *
 * 页账权威：Issue#280 §2.3 / #278 §1 的可导出页口径
 * recognized_page_numbers ∪ manual − failed（后端 `exportable_page_numbers`）。
 */
export function pickSubsetPages(
  status: ItemPageStatus | null | undefined,
  fallbackTotal: number,
): SubsetSelection {
  const total = Math.max(0, Number(fallbackTotal) || 0);
  const pageCount = Math.max(total, Number(status?.page_count) || 0);
  const recognized = new Set<number>();
  const manual = new Set<number>();
  const failed = new Set<number>();

  for (const page of status?.pages ?? []) {
    const pageNo = Number(page?.page);
    if (!Number.isFinite(pageNo) || pageNo <= 0) continue;
    const state = (page as { state?: unknown })?.state;
    if (state === 'success' || state === 'manual_verified') {
      recognized.add(pageNo);
      // 人工补框页：既有人工框优先于空识别结果（#280 红线），不自动翻绿
      if (state === 'manual_verified') manual.add(pageNo);
    }
  }

  for (const pageNo of status?.failed_pages ?? []) {
    const page = Number(pageNo);
    if (Number.isFinite(page) && page > 0) failed.add(page);
  }

  const pages = Array.from(recognized)
    .filter((pageNo) => !failed.has(pageNo))
    .sort((a, b) => a - b);

  return { pages, total: Math.max(1, pageCount) };
}

/** 识别在飞（页账仍可能增长）——子集预览入口的唯一判据（终态必须走全卷链路）。 */
export function isRecognitionInFlight(
  recognitionState: string | null | undefined,
): boolean {
  return recognitionState === 'running';
}

/**
 * 子集入口的候选集：识别在飞 → 已处理页（可导出集）；其余（未开始/已终态/页账缺失）
 * 返回空集 —— 入口不出现，沿用既有全卷链路。
 */
export function subsetCandidates(
  status: ItemPageStatus | null | undefined,
  fallbackTotal: number,
): SubsetSelection {
  const total = Math.max(1, Number(fallbackTotal) || 1);
  if (!status || !isRecognitionInFlight(status.recognition_state)) {
    return { pages: [], total };
  }
  return pickSubsetPages(status, total);
}

/**
 * Issue#327 门⑥ C-2 返工：子集打码的框账合并。
 *
 * 识别在飞时服务端 file_store 已有逐页框（`detect_vision` 边跑边写回），但前端画布
 * 只有用户手改/手拉的框。直接发画布框会把服务端框整份替换掉（丢页），直接不发又会丢掉
 * 手改结果。口径：**画布碰过的页用画布框，其余页用服务端快照框**；画布为空则整份交回
 * 服务端快照（调用方发 undefined，后端从 file_store 兜底）。
 */
export function mergeSubsetBoxes<T extends { page?: number; selected?: boolean }>(
  snapshot: T[],
  canvas: T[],
): T[] {
  if (!canvas.length) return snapshot;
  // 门⑥ 四段 R4-2：touched 取**全量**画布页（含未勾选框）。否则「把某页的框全部取消
  // 勾选」不会被识别成「用户动过这页」⇒ 回落服务端快照框 ⇒ 该页照样被打码，与全卷
  // 链路（`remap_boxes_to_subset` 按 `selected` 过滤）口径不一致。
  const touched = new Set(canvas.map((box) => Number(box.page || 1)));
  const kept = canvas.filter((box) => box.selected !== false);
  return [...snapshot.filter((box) => !touched.has(Number(box.page || 1))), ...kept];
}

/**
 * Issue#327 门⑥ C-4 返工：子集序号 → **原卷页号**。
 *
 * `GET /files/{id}/page-image?preview=true` 的 `page` 参数是**原卷页号**（后端用
 * `page_map` 反查），而结果页的翻页器是子集序号 1..k。`page_map` 是
 * 「子集序号 → 原卷页号」（后端 `subset_preview_service` 的 `{idx + 1: page}`），
 * 这里按子集序号取值；查不到（page_map 缺失/漂移）时退回子集序号原样（前缀子集仍可显示）。
 */
export function outputPreviewPageForRequest(
  page: number,
  pageMap?: Record<string, number> | null,
): number {
  if (!pageMap) return page;
  const mapped = pageMap[String(page)];
  return typeof mapped === 'number' && mapped > 0 ? mapped : page;
}
