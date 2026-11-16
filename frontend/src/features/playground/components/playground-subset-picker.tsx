// Copyright 2026 LexMask Contributors

// 子集预览的选页弹窗（2026-11-16 用户需求：「只要 2、3、4 页」）。
//
// 口径：
//  - 只有**已处理页**（页账 success ∪ manual_verified）可选，未处理页置灰——
//    与后端 `subset_pages_not_ready` 校验同口径，前端先挡一层；
//  - 默认全选已处理页（保持原「先匿名化已处理的 N/M 页」一键语义）；
//  - 选择集是原卷页号，提交后由 `handleRedact(pages)` 原样发给子集预览接口。
//
// 结构：Dialog 外壳 + PickerBody 两段。Radix 关闭时卸载内容，所以 PickerBody 每次
// 打开都是新挂载，`useState` 初值即最新页账——不需要 effect 同步（会多一拍闪烁）。

import { useMemo, useState } from 'react';

import { useT } from '@/i18n';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';

export interface PlaygroundSubsetPickerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** 已处理页（原卷页号，升序）。 */
  pages: readonly number[];
  /** 原卷总页数（页账 page_count 优先，退化到 file_info.page_count）。 */
  total: number;
  /**
   * 识别是否仍在进行（宿主传 `Boolean(taskRecognition)`）。
   * 门⑥ r1 M5：`total > 0` 但候选为空时，要靠它区分「刚开跑还没页」与
   * 「识别刚收尾（候选被清空）」——两者文案不同。
   */
  recognitionActive?: boolean;
  onConfirm: (pages: number[]) => void;
}

export function PlaygroundSubsetPicker({ open, ...rest }: PlaygroundSubsetPickerProps) {
  return (
    <Dialog open={open} onOpenChange={rest.onOpenChange}>
      <DialogContent className="max-w-2xl" data-testid="playground-subset-picker">
        <PickerBody {...rest} />
      </DialogContent>
    </Dialog>
  );
}

function PickerBody({
  pages,
  total,
  recognitionActive,
  onOpenChange,
  onConfirm,
}: Omit<PlaygroundSubsetPickerProps, 'open'>) {
  const t = useT();
  // 防御：上游白名单漏挂时 pages 会是 undefined——这里退化成空态而不是整页崩
  // （本轮实测过这个坑：`Cannot read properties of undefined (reading 'length')`）。
  const readyPages = useMemo(() => (Array.isArray(pages) ? pages : []), [pages]);
  const ready = useMemo(() => new Set(readyPages), [readyPages]);
  const [selected, setSelected] = useState<Set<number>>(() => new Set(readyPages));

  const allPages = useMemo(
    () => Array.from({ length: Math.max(0, Math.floor(total)) }, (_, i) => i + 1),
    [total],
  );
  const ordered = useMemo(() => Array.from(selected).sort((a, b) => a - b), [selected]);

  const toggle = (page: number) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(page)) next.delete(page);
      else next.add(page);
      return next;
    });
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>{t('playground.subsetPickerTitle')}</DialogTitle>
        <DialogDescription data-testid="playground-subset-picker-hint">
          {t('playground.subsetPickerHint')
            .replace('{done}', String(readyPages.length))
            .replace('{total}', String(Math.max(0, Math.floor(total))))}
        </DialogDescription>
      </DialogHeader>

      <div className="flex items-center justify-between gap-2">
        <span
          className="text-xs text-muted-foreground"
          data-testid="playground-subset-picker-count"
        >
          {t('playground.subsetPickerCount')
            .replace('{count}', String(ordered.length))
            .replace('{total}', String(Math.max(0, Math.floor(total))))}
        </span>
        <span className="flex shrink-0 gap-1.5">
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-7"
            onClick={() => setSelected(new Set(readyPages))}
            data-testid="playground-subset-picker-all"
          >
            {t('playground.selectAll')}
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-7"
            onClick={() => setSelected(new Set())}
            data-testid="playground-subset-picker-none"
          >
            {t('playground.deselectAll')}
          </Button>
        </span>
      </div>

      {readyPages.length === 0 ? (
        // 门⑥ r1 M5：`total > 0` 却「没有已处理页」且识别已不在飞 = 识别刚收尾
        // （页账删成空），此时说「还没有已处理的页」与事实相反，得指回全卷入口。
        // 识别仍在进行（刚开跑、0 页完成）⇒ 保持原「稍候再试」文案。
        <p className="text-xs text-muted-foreground" data-testid="playground-subset-picker-empty">
          {!recognitionActive && total > 0
            ? t('playground.subsetPickerFinished')
            : t('playground.subsetPickerEmpty')}
        </p>
      ) : (
        <div
          className="grid max-h-72 grid-cols-8 gap-1.5 overflow-y-auto pr-1 sm:grid-cols-10"
          data-testid="playground-subset-picker-grid"
        >
          {allPages.map((page) => {
            const selectable = ready.has(page);
            const active = selected.has(page);
            return (
              <button
                key={page}
                type="button"
                disabled={!selectable}
                aria-pressed={active}
                aria-label={t('playground.subsetPickerPage').replace('{page}', String(page))}
                onClick={() => toggle(page)}
                className={
                  selectable
                    ? active
                      ? 'h-8 rounded-md border border-primary bg-primary text-xs font-medium text-primary-foreground'
                      : 'h-8 rounded-md border border-border bg-background text-xs text-foreground hover:bg-accent'
                    : 'h-8 cursor-not-allowed rounded-md border border-border/60 bg-muted text-xs text-muted-foreground/60'
                }
                data-testid={`playground-subset-page-${page}`}
                data-selectable={selectable ? 'true' : 'false'}
              >
                {page}
              </button>
            );
          })}
        </div>
      )}

      <DialogFooter>
        <Button
          type="button"
          variant="outline"
          onClick={() => onOpenChange(false)}
          data-testid="playground-subset-picker-cancel"
        >
          {t('common.cancel')}
        </Button>
        <Button
          type="button"
          disabled={ordered.length === 0}
          onClick={() => {
            onConfirm(ordered);
            onOpenChange(false);
          }}
          data-testid="playground-subset-picker-confirm"
        >
          {t('playground.subsetPickerConfirm').replace('{count}', String(ordered.length))}
        </Button>
      </DialogFooter>
    </>
  );
}
