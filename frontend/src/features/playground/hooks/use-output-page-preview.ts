// Copyright 2026 LexMask Contributors

import { useEffect, useReducer, useRef } from 'react';
import { authFetch } from '@/services/api-client';
import { t } from '@/i18n';
import { localizeErrorMessage } from '@/utils/localizeError';

const CACHE_LIMIT = 24;

export interface UseOutputPagePreviewOptions {
  fileId: string | null;
  page: number;
  /** false 时不发请求（成品视图切换为懒加载，回对照视图即停） */
  enabled: boolean;
}

export interface OutputPagePreviewState {
  url: string;
  pageCount: number | null;
  loading: boolean;
  error: string | null;
}

interface CacheEntry {
  url: string;
  pageCount: number | null;
}

/**
 * 成品页图（Issue #83）：GET /files/{id}/page-image?redacted=true 的浏览器侧
 * 封装——blob URL + X-Page-Count。
 *
 * 页图/页数/错误全部挂在 ref 缓存、渲染期按 key 派生，提交只发生在异步回
 * 调里——effect 内不做同步 setState（react-hooks 级联渲染警告）。未命中缓
 * 存且无错误即视为加载中（首帧即显示加载态，无需额外 state 触发重渲染）。
 * 每次执行后结果页整体卸载重挂（stage 切换），组件生命周期即缓存生命周期，
 * 无需版本键。
 */
export function useOutputPagePreview({
  fileId,
  page,
  enabled,
}: UseOutputPagePreviewOptions): OutputPagePreviewState {
  const cacheRef = useRef<Map<string, CacheEntry>>(new Map());
  const errorsRef = useRef<Map<string, string>>(new Map());
  const inflightRef = useRef<Set<string>>(new Set());
  const epochRef = useRef(0);
  const [, forceRender] = useReducer((count: number) => count + 1, 0);

  const activeKey = enabled && fileId ? `${fileId}:${page}` : null;
  const cached = activeKey ? cacheRef.current.get(activeKey) : undefined;
  const cachedError = activeKey ? errorsRef.current.get(activeKey) : undefined;

  useEffect(() => {
    // 三个 Map 实例终生不变（只变内容），按 lint 建议在 effect 内捕获引用
    const cache = cacheRef.current;
    const errors = errorsRef.current;
    const inflight = inflightRef.current;
    return () => {
      // 卸载（含重执行后结果页重挂）：回收全部 blob URL 并作废在途回调
      epochRef.current += 1;
      for (const entry of cache.values()) URL.revokeObjectURL(entry.url);
      cache.clear();
      errors.clear();
      inflight.clear();
    };
  }, []);

  useEffect(() => {
    if (!activeKey || cached || cachedError !== undefined || inflightRef.current.has(activeKey)) {
      return;
    }
    const key = activeKey;
    const epoch = ++epochRef.current;
    inflightRef.current.add(key);
    authFetch(`/api/v1/files/${fileId}/page-image?page=${page}&redacted=true`)
      .then(async (res) => {
        if (!res.ok) {
          // 400=尚未匿名化（成品不存在）；其余按渲染失败兜底
          throw new Error(
            res.status === 400
              ? t('playground.outputPreview.notReady')
              : t('playground.outputPreview.failed'),
          );
        }
        const header = res.headers.get('X-Page-Count');
        const count = header ? Math.max(1, Number(header) || 1) : null;
        const blob = await res.blob();
        return { objectUrl: URL.createObjectURL(blob), count };
      })
      .then(({ objectUrl, count }) => {
        inflightRef.current.delete(key);
        if (epoch !== epochRef.current) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        const cache = cacheRef.current;
        cache.delete(key);
        cache.set(key, { url: objectUrl, pageCount: count });
        while (cache.size > CACHE_LIMIT) {
          const oldest = cache.keys().next().value;
          if (oldest === undefined) break;
          const evicted = cache.get(oldest);
          cache.delete(oldest);
          if (evicted) URL.revokeObjectURL(evicted.url);
        }
        forceRender();
      })
      .catch((err) => {
        inflightRef.current.delete(key);
        if (epoch !== epochRef.current) return;
        errorsRef.current.set(key, localizeErrorMessage(err, 'playground.outputPreview.failed'));
        forceRender();
      });
    // 缓存/在途/错误表都在 ref 里，key 已含 fileId 与页码
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey]);

  return {
    url: cached?.url ?? '',
    pageCount: cached?.pageCount ?? null,
    loading: !!activeKey && !cached && cachedError === undefined,
    error: cachedError ?? null,
  };
}
