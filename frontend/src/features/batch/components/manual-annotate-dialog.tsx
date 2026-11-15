// Copyright 2026 LexMask Contributors

// Issue#280 能力 4：失败页人工拉框兜底对话框。
//
// 复用审阅画布（ImageBBoxEditor）的框编辑能力：拉框、选类型，文本可选
// （Ruling：框必填、文本可选——text 缺省照常图形遮蔽打码）。
// 页图走 GET /files/{file_id}/page-image?page={p}，**不传 raw=true**——默认
// 净化副本即识别实际吃到的页图（Issue#236 裁定），人工框与识别框同坐标系。
// 提交走 manualVerifyPage（POST manual-verify）；错误码走 batchWizard.pages.
// manualError.* 文案（未知码回落通用文案，同 retryErrorMessage 模式）。

import { useCallback, useEffect, useState } from 'react';

import { useT } from '@/i18n';
import { authFetch, ApiError } from '@/services/api-client';
import {
  manualVerifyPage,
  type ManualBoxPayload,
  type ManualVerifyResult,
} from '@/services/jobsApi';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import ImageBBoxEditor, { type BoundingBox as CanvasBox } from '@/components/ImageBBoxEditor';

import type { PipelineCfg } from '../types';

// 与 review-image-content.tsx 同款默认色（不引新依赖）。
const DEFAULT_VISION_TYPE_COLOR = '#6366F1';

interface ManualAnnotateDialogProps {
  jobId: string;
  itemId: string;
  fileId: string;
  page: number;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** verify 成功（父级刷新 page-status + toast）。 */
  onVerified: (page: number) => void;
  /** 视觉类型表（可选）：构建 getTypeConfig 的人话名与色板；缺省按原 id 直显。 */
  pipelines?: PipelineCfg[];
  /** 画布初始框（测试播种 / 重验时回填）；对话框内部仍持有草稿状态。 */
  initialBoxes?: CanvasBox[];
}

/** 把后端错误码翻成人话；未知码带上原文（同 retryErrorMessage 模式）。 */
function manualErrorMessage(t: (key: string) => string, err: unknown): string {
  if (err instanceof ApiError) {
    const key = `batchWizard.pages.manualError.${err.errorCode || 'unknown'}`;
    const text = t(key);
    if (text !== key) return text;
    return t('batchWizard.pages.manualError.unknown').replace(
      '{message}',
      err.message || err.errorCode || String(err.status),
    );
  }
  return t('batchWizard.pages.manualError.unknown').replace('{message}', String(err));
}

export function ManualAnnotateDialog({
  jobId,
  itemId,
  fileId,
  page,
  open,
  onOpenChange,
  onVerified,
  pipelines = [],
  initialBoxes = [],
}: ManualAnnotateDialogProps) {
  const t = useT();
  const [imageUrl, setImageUrl] = useState('');
  const [imageError, setImageError] = useState<string | null>(null);
  const [boxes, setBoxes] = useState<CanvasBox[]>(initialBoxes);
  const [submitting, setSubmitting] = useState(false);
  const [errorText, setErrorText] = useState<string | null>(null);

  // 每次打开重置草稿与错误面（对话框单页无翻页，从简无缓存）。
  useEffect(() => {
    if (open) {
      setBoxes(initialBoxes);
      setErrorText(null);
      setImageError(null);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, page]);

  // 页图加载（净化副本，不传 raw=true）。对话框单页无翻页 ⇒ 不做 LRU 缓存。
  useEffect(() => {
    if (!open) return;
    let objectUrl = '';
    let cancelled = false;
    setImageError(null);
    authFetch(`/api/v1/files/${encodeURIComponent(fileId)}/page-image?page=${page}`)
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(`page-image ${res.status}`);
        }
        const blob = await res.blob();
        objectUrl = URL.createObjectURL(blob);
        if (cancelled) {
          URL.revokeObjectURL(objectUrl);
          return;
        }
        setImageUrl(objectUrl);
      })
      .catch(() => {
        if (!cancelled) setImageError(t('batchWizard.pages.manualDialogLoadError'));
      });
    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [open, fileId, page, t]);

  // 与 review-image-content 的 visionTypeMetaById 同构（pipelines 类型表 + 默认色）。
  const getTypeConfig = useCallback(
    (typeId: string) => {
      for (const pipeline of pipelines) {
        const type = pipeline.types.find((item) => item.id === typeId);
        if (type) return { name: type.name, color: type.color || DEFAULT_VISION_TYPE_COLOR };
      }
      return { name: typeId, color: DEFAULT_VISION_TYPE_COLOR };
    },
    [pipelines],
  );

  const handleBoxesChange = useCallback(
    (next: CanvasBox[]) => {
      // 人工框统一 source=manual（服务端也会强制，画布侧先行标注保证预览一致）。
      setBoxes(next.map((box) => ({ ...box, source: 'manual' as const })));
    },
    [],
  );

  const save = useCallback(async () => {
    if (boxes.length === 0 || submitting) return;
    setSubmitting(true);
    setErrorText(null);
    const payload: ManualBoxPayload[] = boxes.map((box) => ({
      x: box.x,
      y: box.y,
      width: box.width,
      height: box.height,
      type: box.type,
      text: box.text || undefined,
    }));
    try {
      const result: ManualVerifyResult = await manualVerifyPage(jobId, itemId, page, payload);
      onVerified(result.page);
      onOpenChange(false);
    } catch (err) {
      setErrorText(manualErrorMessage(t, err));
    } finally {
      setSubmitting(false);
    }
  }, [boxes, submitting, jobId, itemId, page, onVerified, onOpenChange, t]);

  const updateBoxText = (boxId: string, text: string) => {
    setBoxes((prev) =>
      prev.map((box) => (box.id === boxId ? { ...box, text: text || undefined } : box)),
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-3xl" data-testid="manual-annotate-dialog">
        <DialogHeader>
          <DialogTitle>
            {t('batchWizard.pages.manualDialogTitle').replace('{page}', String(page))}
          </DialogTitle>
          <DialogDescription>{t('batchWizard.pages.manualDialogHint')}</DialogDescription>
        </DialogHeader>
        {imageError ? (
          <p className="text-xs text-destructive" data-testid="manual-annotate-image-error">
            {imageError}
          </p>
        ) : imageUrl ? (
          <div className="max-h-[55vh] overflow-hidden">
            <ImageBBoxEditor
              imageSrc={imageUrl}
              boxes={boxes}
              onBoxesChange={handleBoxesChange}
              getTypeConfig={getTypeConfig}
            />
          </div>
        ) : (
          <p className="text-xs text-muted-foreground" data-testid="manual-annotate-image-loading">
            {t('batchWizard.pages.loading')}
          </p>
        )}
        {boxes.length > 0 && (
          <div className="flex flex-col gap-1" data-testid="manual-annotate-box-list">
            {boxes.map((box, index) => (
              <div key={box.id} className="flex items-center gap-2 text-xs">
                <span className="w-24 shrink-0 truncate">
                  {index + 1}. {getTypeConfig(box.type).name}
                </span>
                <Input
                  className="h-7 text-xs"
                  placeholder={t('batchWizard.pages.manualTextPlaceholder')}
                  value={box.text ?? ''}
                  onChange={(event) => updateBoxText(box.id, event.target.value)}
                  data-testid={`manual-annotate-box-text-${box.id}`}
                  aria-label={t('batchWizard.pages.manualTextPlaceholder')}
                />
              </div>
            ))}
          </div>
        )}
        {errorText && (
          <p className="text-xs text-destructive" data-testid="manual-annotate-error">
            {errorText}
          </p>
        )}
        <div className="flex items-center justify-between gap-2">
          <span className="text-xs text-muted-foreground" data-testid="manual-annotate-count">
            {t('batchWizard.pages.manualBoxCount').replace('{count}', String(boxes.length))}
          </span>
          <div className="flex gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 text-xs"
              onClick={() => onOpenChange(false)}
              data-testid="manual-annotate-cancel"
            >
              {t('pageDetail.close')}
            </Button>
            <Button
              type="button"
              size="sm"
              className="h-8 text-xs"
              disabled={boxes.length === 0 || submitting}
              onClick={() => void save()}
              data-testid="manual-annotate-save"
            >
              {submitting
                ? t('batchWizard.pages.manualSaving')
                : t('batchWizard.pages.manualSave')}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
