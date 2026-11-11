// Copyright 2026 LexMask Contributors

import type { FC } from 'react';
import type { KeyboardEvent, MouseEvent } from 'react';
import { Button } from '@/components/ui/button';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import type { usePlayground } from '../hooks/use-playground';

type PlaygroundCtx = ReturnType<typeof usePlayground>;

interface PlaygroundUploadDropzoneProps {
  dropzone: PlaygroundCtx['dropzone'];
  disabled?: boolean;
  disabledReason?: string;
  uploadIssue?: string | null;
}

export const PlaygroundUploadDropzone: FC<PlaygroundUploadDropzoneProps> = ({
  dropzone,
  disabled = false,
  disabledReason,
  uploadIssue,
}) => {
  const t = useT();
  const { getRootProps, getInputProps, isDragActive, open } = dropzone;
  const handleOpenClick = (e: MouseEvent) => {
    e.stopPropagation();
    if (disabled) {
      e.preventDefault();
      return;
    }
    open();
  };
  const handleRootKeyDown = (e: KeyboardEvent) => {
    if (!disabled) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      e.stopPropagation();
    }
  };

  return (
    // Issue#262 Task 9b：上传大卡（界面稿形态）——旧组件内大标题/长说明/装饰性格式行
    // 移除（页头紧凑化后由页面页头承担），高度下限收敛到外层容器，卡内只留拖入提示/支持格式。
    <div className="flex min-h-[18rem] min-w-0 flex-1 items-stretch lg:min-h-[20rem] 2xl:min-h-[22rem]">
      <div
        {...getRootProps({
          onClick: handleOpenClick,
          onKeyDown: handleRootKeyDown,
          tabIndex: disabled ? -1 : 0,
        })}
        aria-label={disabledReason || t('playground.dropHere')}
        aria-disabled={disabled}
        className={cn(
          'saas-hero group relative w-full cursor-pointer border-2 border-dashed p-7 text-center transition-all duration-300 ease-out lg:px-10 2xl:p-9 2xl:px-12',
          isDragActive
            ? 'border-primary bg-primary/[0.04] ring-4 ring-primary/10'
            : 'border-border hover:border-foreground/15 hover:shadow-lg',
          disabled && 'cursor-not-allowed opacity-65 hover:border-border hover:shadow-none',
        )}
        data-testid="playground-dropzone"
      >
        <input {...getInputProps({ 'aria-label': t('playground.clickToUpload'), disabled })} />
        <div className="flex h-full flex-col items-center justify-center">
          <div
            className={cn(
              'mx-auto mb-4 flex size-[3.75rem] items-center justify-center rounded-[20px] bg-foreground text-background transition-transform duration-300 group-hover:scale-110',
              isDragActive && 'scale-110 animate-pulse',
            )}
          >
            <svg className="size-8" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={1.5}
                d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"
              />
            </svg>
          </div>
          <p className="mb-1.5 text-lg font-semibold tracking-[-0.02em]">
            {t('playground.dropHere')}
          </p>
          <p className="mb-5 text-sm text-muted-foreground">{t('playground.supportedFormats')}</p>
          {uploadIssue && (
            <div
              className="mb-4 max-w-md rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs font-medium text-destructive"
              role="alert"
              data-testid="playground-upload-issue"
            >
              {uploadIssue}
            </div>
          )}
          <Button type="button" variant="outline" size="sm" onClick={handleOpenClick} disabled={disabled}>
            <svg className="size-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24">
              <path
                strokeLinecap="round"
                strokeLinejoin="round"
                strokeWidth={2}
                d="M12 4v16m8-8H4"
              />
            </svg>
            {t('playground.clickToUpload')}
          </Button>
        </div>
      </div>
    </div>
  );
};
