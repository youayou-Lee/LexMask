// Copyright 2026 LexMask Contributors

// Issue#257 需求重对齐（2026-11-11）：单文件任务（/single）也要能看逐页结果、
// 选页导出。单文件流程手里只有 fileId，需要先解出 (jobId, itemId) 才能问
// /jobs/{jobId}/items/{itemId}/page-status。
//
// 解析链（按可靠度递降）：
//  1. provider 的 taskForFile —— 识别中/刚完成时最准；
//  2. 按文件 sticky —— provider 账本终态短留后会清条目，兜住本会话内的重渲染；
//  3. 草稿里的 taskJobId —— 刷新后恢复；
//  4. `/jobs/items/by-file/{fileId}` —— 前三条都为空时（账本已清 + 草稿也缺
//     jobId），由 job 账本按 file_id 反查，保证刷新后页格仍然在。
// itemId 再从轻量 getJob(performance:false) 的 items 里按 file_id 认领。

import { useEffect, useState } from 'react';

import { getItemRefByFile, getJob } from '@/services/jobsApi';
import { useSingleTask } from '@/features/single-task/single-task-context';
import { STORAGE_KEYS } from '@/constants/storage-keys';
import { getScopedStorageItem } from '@/lib/storage';
import type { PlaygroundDraftSnapshot } from '../lib/playground-draft';

function draftTaskJobIdFor(fileId: string): string | null {
  const snapshot = getScopedStorageItem<PlaygroundDraftSnapshot | null>(
    STORAGE_KEYS.PLAYGROUND_DRAFT,
    null,
  );
  if (!snapshot || snapshot.fileInfo?.file_id !== fileId) return null;
  return typeof snapshot.taskJobId === 'string' && snapshot.taskJobId ? snapshot.taskJobId : null;
}

export interface FileJobItem {
  jobId: string | null;
  itemId: string | null;
}

export function useFileJobItem(fileId: string | null, enabled = true): FileJobItem {
  const { taskForFile } = useSingleTask();
  const providerJobId = fileId ? (taskForFile(fileId)?.jobId ?? null) : null;

  const [stickyByFile, setStickyByFile] = useState<Record<string, string>>({});
  useEffect(() => {
    if (!fileId || !providerJobId) return;
    setStickyByFile((prev) =>
      prev[fileId] === providerJobId ? prev : { ...prev, [fileId]: providerJobId },
    );
  }, [fileId, providerJobId]);

  const knownJobId =
    providerJobId ??
    (fileId ? (stickyByFile[fileId] ?? null) : null) ??
    (fileId ? draftTaskJobIdFor(fileId) : null);

  // 前三条都解析不出时，向 job 账本按 file_id 反查（只做一次，结果也进 sticky）。
  const [lookedUp, setLookedUp] = useState<{ fileId: string; jobId: string; itemId: string } | null>(
    null,
  );
  useEffect(() => {
    if (!enabled || !fileId || knownJobId) return;
    let cancelled = false;
    getItemRefByFile(fileId)
      .then((ref) => {
        if (cancelled || !ref.job_id || !ref.item_id) return;
        setLookedUp({ fileId, jobId: ref.job_id, itemId: ref.item_id });
      })
      .catch(() => {
        /* 查不到就当没有历史：页格不渲染，不打断结果页 */
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, fileId, knownJobId]);

  const lookup = lookedUp && lookedUp.fileId === fileId ? lookedUp : null;
  const jobId = knownJobId ?? lookup?.jobId ?? null;

  const [itemId, setItemId] = useState<string | null>(null);
  useEffect(() => {
    if (lookup && lookup.jobId === jobId) {
      setItemId(lookup.itemId);
      return;
    }
    setItemId(null);
    if (!enabled || !jobId || !fileId) return;
    let cancelled = false;
    getJob(jobId, { performance: false })
      .then((detail) => {
        if (cancelled) return;
        const item = (detail.items ?? []).find((row) => row.file_id === fileId);
        setItemId(item?.id ?? null);
      })
      .catch(() => {
        /* 解析失败就当没页级视图：页格不渲染，不打断结果页 */
      });
    return () => {
      cancelled = true;
    };
  }, [enabled, fileId, jobId, lookup]);

  return { jobId, itemId };
}