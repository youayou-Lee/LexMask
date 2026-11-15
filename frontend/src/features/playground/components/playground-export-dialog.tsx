// Copyright 2026 LexMask Contributors

// Issue#313（门⑥ I-2）：单文件复核阶段的「逐页结果与选页导出」弹窗外壳。
//
// 之前宿主把 `<Dialog>` + `<PlaygroundPageExport bare />` 直接写进 playground-page.tsx，
// 没有任何测试渲染这个宿主——把 onRequestClose 删掉也全绿。抽成独立组件后，可以真渲染
// 一个 Radix Dialog 来钉住「点回到编辑 → onOpenChange(false) → 弹窗关闭」。

import { useCallback, useRef, type FC } from 'react';

import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { useT } from '@/i18n';

import { PlaygroundPageExport } from './playground-page-export';

export interface PlaygroundExportDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  fileId: string | null;
}

export const PlaygroundExportDialog: FC<PlaygroundExportDialogProps> = ({
  open,
  onOpenChange,
  fileId,
}) => {
  const t = useT();
  // 只有「回到编辑」这条路径要阻止 Radix 关闭时把焦点还给触发按钮——画布已由导出块
  // 在下一帧主动聚焦（门⑥ M-3：onUnmountAutoFocus 晚于 rAF，会把焦点抢回去）。
  const backToEditorRef = useRef(false);

  const handleBackToEdit = useCallback(() => {
    backToEditorRef.current = true;
    onOpenChange(false);
  }, [onOpenChange]);

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        // X / ESC / 遮罩关闭：恢复 Radix 默认焦点归还，不吞掉可访问性行为。
        if (!next) backToEditorRef.current = false;
        onOpenChange(next);
      }}
    >
      <DialogContent
        className="max-w-3xl"
        data-testid="playground-export-pages-dialog"
        onCloseAutoFocus={(event) => {
          if (!backToEditorRef.current) return;
          backToEditorRef.current = false;
          event.preventDefault();
        }}
      >
        <DialogTitle>{t('batchWizard.pages.singleTitle')}</DialogTitle>
        <div className="max-h-[70vh] overflow-y-auto">
          <PlaygroundPageExport fileId={fileId} bare onRequestClose={handleBackToEdit} />
        </div>
      </DialogContent>
    </Dialog>
  );
};