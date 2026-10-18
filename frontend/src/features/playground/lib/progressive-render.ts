// Copyright 2026 LexMask Contributors

/**
 * Issue#123：识别期间渐进渲染的判据（纯函数，供表现层与单测共用）。
 *
 * 背景：多页扫描件首次上传时 `stage` 直到识别成功才切 `preview`
 * （见 `use-playground-file.ts` 的 doRecognition 末尾），整卷识别期间结果画布不在渲染树里，
 * 逐页回流的框无处可显 ⇒ 首屏 = 全卷（10 页 canary 实测 86.74s，而第 1 页 8.7s 就已完成）。
 */

/** 流式进度：已完成页数与总页数。 */
export interface VisionStreamProgress {
  completed: number;
  total: number;
}

/**
 * 多页扫描件/图片在 parse 完成后是否**立即**进入 `preview`：
 * 让画布在识别期间挂载，逐页回流的框可见。
 * 单页件与文本件保持既有行为（识别结束才切 preview）。
 */
export function shouldEnterPreviewDuringRecognition(input: {
  pageCount: number;
  isScanned: boolean;
  fileType: string;
}): boolean {
  const { pageCount, isScanned, fileType } = input;
  return pageCount > 1 && (isScanned || fileType === 'image');
}

/**
 * 是否把全屏遮罩降级为 `passive`（透明、不遮挡、不锁交互）。
 * - 多页流式识别中（Issue#123）：正在加载且逐页进度已登记；
 * - 后台任务识别中（Issue#194 `taskDriven`）：进度来自轮询而非前端在飞请求，
 *   画布/文本均已就位，前端没有可遮挡的「正在进行」画面 ⇒ 一律 passive。
 * 其余（单页扫描件 / 图片单页 / 非流式路径且非任务驱动）保持 `blocking`
 * ⇒ 与改前的遮罩行为一致。
 */
export function shouldUsePassiveOverlay(
  isLoading: boolean,
  stream: VisionStreamProgress | null,
  taskDriven = false,
): boolean {
  if (isLoading && taskDriven) return true;
  return isLoading && stream !== null && stream.total > 1;
}

/** 遮罩档位：`blocking`＝全屏半透明锁交互；`passive`＝透明不遮挡。 */
export type OverlayMode = 'blocking' | 'passive';

/**
 * 表现层实传给 `<PlaygroundLoading overlay=…>` 的档位。
 *
 * 单独抽成纯函数而不是在 JSX 里写三元：接线层的取值必须可被单测判别
 * （Issue#123 门①.5 r2：内联三元的接线事实曾无任何判据）。
 */
export function overlayModeFor(progressiveRecognition: boolean): OverlayMode {
  return progressiveRecognition ? 'passive' : 'blocking';
}
