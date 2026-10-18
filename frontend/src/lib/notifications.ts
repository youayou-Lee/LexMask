// Copyright 2026 LexMask Contributors

import { showToast } from '@/components/Toast';

/**
 * 任务完成通知（W2-2）：万级批量跑十几分钟，用户不会盯屏。
 * 页内永远 toast；页签隐藏时追加系统通知（权限需在用户手势里预请求）。
 */

/** 纯函数：活跃数从 >0 归 0 即视为该阶段完成（供 vitest 与调用方复用）。 */
export function phaseJustFinished(prevActive: number, nextActive: number): boolean {
  return prevActive > 0 && nextActive === 0;
}

/** 在用户手势上下文（如点击「开始识别」）里预请求系统通知权限。 */
export function ensureNotifyPermission(): void {
  try {
    if (typeof window === 'undefined' || !('Notification' in window)) return;
    if (Notification.permission === 'default') {
      void Notification.requestPermission();
    }
  } catch {
    /* 通知权限失败不影响业务 */
  }
}

/**
 * 完成通知：toast 常显；页签隐藏时再发系统通知。
 * Issue#194 终审 D（AC2）：onClick 让 toast 可点击直达（/single?file_id=）。
 * 系统通知（页签隐藏时）降级为不可点击——浏览器 Notification 的 onclick 回调
 * 接不进 SPA 路由（无 React 树上下文），聚焦页签后由页面内 toast 承担直达。
 */
export function notifyDone(title: string, body?: string, onClick?: () => void): void {
  showToast(body ? `${title}：${body}` : title, 'success', onClick ? { onClick } : undefined);
  try {
    if (typeof document === 'undefined' || document.visibilityState === 'visible') return;
    if (typeof window === 'undefined' || !('Notification' in window)) return;
    if (Notification.permission === 'granted') {
      new Notification(title, body ? { body } : undefined);
    }
  } catch {
    /* 系统通知失败静默，toast 已兜底 */
  }
}
