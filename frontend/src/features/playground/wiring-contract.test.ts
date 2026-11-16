// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

// Issue#123 门⑥ I-1 / I-2：收益链的**接线层**此前单测零覆盖。
// 评审实测：删掉第一层根因 `setStage('preview')`（自造变异 M15）或删掉 `setVisionStream` 登记（M16），
// `npm test` 照样 157 passed；把 `overlay={overlayModeFor(progressiveRecognition)}` 写死 `'blocking'`（M6）
// 或把 `lockDraw` 里的 `progressiveRecognition` 摘掉（M10）同样全绿。
//
// 本用例是**表达式级契约测试**：把四处接线在源码里钉住。它不替代行为验证——
// 行为面由 `utils.vision-stream-order.test.ts`（数据层顺序）与门④ 浏览器臂（真 DOM 可见性）承担。
// 诚实边界：表达式改名/重构会让本用例变红（这是设计意图：静默丢接线必须被拦住），
// 因此改接线时必须同步改本文件。
// Issue#194 T9：识别页改后台任务流，「文件路径」一组的钉同步换成了任务流接线
// （submitRecognitionTask/watch/进度映射/cancelJob），旧直调（ner/hybrid、runVisionDetectionPages）
// 在 fileHook 里钉为**不得出现**，并在 imageHook 钉为**保留**（AC5 红线）。

// Issue#157：源码读取改走 Vite 的 `?raw` 导入（vitest 原生支持），不再用 `node:fs`/`node:url`。
// 原因：引入 `@types/node` 后全局 `setTimeout` 的重载来自它，三处
// `ReturnType<typeof window.setTimeout>` 的**声明侧**会被 tsc 解析成 `NodeJS.Timeout`，
// 而 `window.setTimeout(...)` 的实参返回仍是 `number` ⇒ 三个产品文件报 TS2322
// （类型门不该改产品类型面）。
// 注意：`eager: true` 是本用例的前提——空集时下面的 `read()` 守卫必须能报到，勿删。
const sources = import.meta.glob(
  './{hooks/use-playground-file.ts,hooks/use-playground-image.ts,hooks/use-playground.ts,playground-page.tsx,playground-context.tsx,components/playground-page-export.tsx}',
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>;

const read = (relative: string): string => {
  const content = sources[`./${relative.replace(/^\.\//, '')}`];
  if (typeof content !== 'string') {
    throw new Error(`wiring-contract: 读不到源码 ${relative}（可用键：${Object.keys(sources).join(', ')}）`);
  }
  return content;
};

const fileHook = read('./hooks/use-playground-file.ts');
const imageHook = read('./hooks/use-playground-image.ts');
const playgroundHook = read('./hooks/use-playground.ts');
const page = read('./playground-page.tsx');
const context = read('./playground-context.tsx');
// 2026-11-16：识别期浮窗紧凑页格已删——钉住 `compact` 不得复活。
const pageExport = read('./components/playground-page-export.tsx');

describe('Issue#123 接线契约：四处接线不得被静默摘掉', () => {
  it('第一层根因：多页扫描在识别期即进入 preview（M15 若删掉，这里红）', () => {
    expect(fileHook).toContain("import { shouldEnterPreviewDuringRecognition } from '../lib/progressive-render';");
    // 判定谓词 + 紧邻的 stage 翻转必须成对出现（只留谓词不留 setStage 等于零收益）
    expect(fileHook).toMatch(
      /shouldEnterPreviewDuringRecognition\(\{[\s\S]{0,200}?\}\)\s*\)\s*\{\s*\n\s*setStage\('preview'\);/,
    );
  });

  it('第二层根因：遮罩 overlay 由 overlayModeFor(progressiveRecognition) 决定（M6 若写死 blocking，这里红）', () => {
    expect(page).toContain('overlay={overlayModeFor(progressiveRecognition)}');
    expect(page).toMatch(/import \{[^}]*overlayModeFor[^}]*\} from '\.\/lib\/progressive-render';/);
    expect(page).toContain('lockDraw={replacePageView || progressiveRecognition}');
  });

  it('识别期画布只读：progressiveRecognition 必须参与 lockDraw（M10 若摘掉，这里红）', () => {
    // Issue#194：shouldUsePassiveOverlay 增加第三参 taskDriven——任务驱动的识别一律 passive
    expect(page).toContain(
      'const progressiveRecognition = shouldUsePassiveOverlay(isLoading, visionStream, taskRecognition !== null);',
    );
    // 真进度接线：遮罩面板进度条吃 visionStream（页流式与任务轮询共用管道）
    expect(page).toContain('progress={visionStream}');
  });

  it('Issue#194：文件识别流改后台任务——提交/watch/进度映射/取消的接线钉', () => {
    // 识别页本流程不再直调旧端点/旧函数（AC5：函数与端点保留给重跑等调用方，见下一用例）
    expect(fileHook).not.toContain('runVisionDetectionPages');
    expect(fileHook).not.toContain('ner/hybrid');
    expect(fileHook).toContain(
      "import { resolveAwaitingReviewTask, submitRecognitionTask } from '@/services/singleTaskApi';",
    );
    // Issue#194 终审 B：扫描件/图片的视觉要素选择随任务透传（isVisionFile 三参分支）
    expect(fileHook).toMatch(
      /await submitRecognitionTask\(\s*\n\s*fileId,\s*\n\s*opts\.latestSelectedTypesRef\.current,\s*\n\s*isVisionFile\s*\n\s*\? \{\s*\n\s*ocrHasTypes: opts\.latestOcrHasTypesRef\.current,\s*\n\s*visualFeatureTypes: skipVisual\s*\n\s*\? \[\]\s*\n\s*: opts\.latestVisualFeatureTypesRef\.current,\s*\n\s*\}\s*\n\s*: undefined,\s*\n\s*\);/,
    );
    // LA 未启动跳过视觉段的判据与提示接线不得被静默摘掉
    expect(fileHook).toContain("Boolean(opts.isVisualServiceBlocked?.())");
    expect(fileHook).toContain("setVisualSkippedNotice(t('playground.visualSkippedNotice'))");
    // rerun 路径同语义（门⑥ I1）：跳过判据 + 提示刷新不得被静默摘掉
    expect(playgroundHook).toContain('skipVisual ? [] : recognition.selectedVisualFeatureTypes');
    expect(playgroundHook).toContain("setVisualSkippedNotice(skipVisual ? t('playground.visualSkippedNotice') : null)");
    // provider watch 注册（占位视图即刻可被 taskForFile 命中）
    expect(fileHook).toMatch(/watch\(ref\.job_id, fileId,/);
    // 阶段人话 + 真进度映射（字段换源：任务轮询的 progress_current/total → visionStream）
    // Issue#339：提交在途（未受理）必须是 taskSubmitting，受理后解析段才是 taskParsing
    expect(fileHook).toContain("setLoadingMessage(t('playground.taskSubmitting'));");
    expect(fileHook).toContain("setLoadingMessage(t('playground.taskParsing'));");
    expect(fileHook).toContain("t('playground.taskRecognizing')");
    expect(fileHook).toMatch(/setVisionStream\(\{ completed: task\.current, total: task\.total \}\);/);
    // awaiting_review 迁移只执行一次（handledJobRef 守卫）
    expect(fileHook).toMatch(/handledJobRef\.current === task\.jobId\) return;/);
    // 取消走 cancelJob（后端标 cancelled ⇒ provider 报 failed，前端静默）
    expect(fileHook).toContain('void cancelJob(active.jobId).catch(() => {');
  });

  it('重跑路径（图片模式）：同样登记/更新/清空 visionStream', () => {
    expect(imageHook).toMatch(/setVisionStream\?: \(v: \{ completed: number; total: number \} \| null\) => void,/);
    expect(imageHook).toMatch(
      /setVisionStream\?\.\(Math\.max\(1, totalPages\) > 1 \? \{ completed: 0, total: totalPages \} : null\);/,
    );
    expect(imageHook).toMatch(/setVisionStream\?\.\(\{ completed: completedPages, total: totalPages \}\);/);
    expect(imageHook).toMatch(/setVisionStream\?\.\(null\);/);
    // AC5 红线：runVisionDetectionPages 保留（重跑路径仍在用）
    expect(imageHook).toContain('runVisionDetectionPages');
  });

  it('上下文贯通：hook 透传 setVisionStream / visionStream 且进入依赖数组', () => {
    expect(playgroundHook).toMatch(/fileCtx\.setVisionStream,/);
    expect(playgroundHook).toMatch(/visionStream: fileCtx\.visionStream,/);
    expect(context).toContain("visionStream: PlaygroundContextValue['visionStream'];");
    expect(context).toMatch(/visionStream: ctx\.visionStream,/);
    expect(context).toMatch(/ctx\.visionStream,/);
  });

  it('Issue#194：taskRecognition 贯通 hook → context → page，草稿带 taskJobId', () => {
    expect(playgroundHook).toMatch(/taskRecognition: fileCtx\.taskRecognition,/);
    expect(playgroundHook).toMatch(/taskJobId: fileCtx\.taskRecognition\?\.jobId,/);
    expect(playgroundHook).toMatch(/fileCtx\.reattachTask\(/);
    expect(context).toContain("taskRecognition: PlaygroundContextValue['taskRecognition'];");
    expect(context).toMatch(/taskRecognition: ctx\.taskRecognition,/);
    expect(context).toMatch(/ctx\.taskRecognition,/);
  });

  it('Issue#194 Task11：taskPerformance 贯通 hook → context → page → loading（三处一致性接线）', () => {
    // hook 层：provider view 的 performance 登记与退出清空（识别中）/ 透传
    expect(fileHook).toMatch(/setTaskPerformance\(task\.performance \?\? null\);/);
    expect(playgroundHook).toMatch(/taskPerformance: fileCtx\.taskPerformance,/);
    // context 层：类型 + 映射 + 依赖数组
    expect(context).toContain("taskPerformance: PlaygroundContextValue['taskPerformance'];");
    expect(context).toMatch(/taskPerformance: ctx\.taskPerformance,/);
    expect(context).toMatch(/ctx\.taskPerformance,/);
    // 页面层：loading 拿到 performance（任务驱动识别期间才非 null）
    expect(page).toMatch(/performance=\{taskRecognition \? taskPerformance : null\}/);
  });

  it('Issue#238：taskSegment/taskInFlight/taskStartedAt/taskTextMode/redactProgress 贯通 hook → context → page（五字段接线钉）', () => {
    // hook 层（use-playground-file）：新状态 + recognizing 分支计算 + return 透出
    expect(fileHook).toMatch(/currentSegmentFromPerformance\(task\.performance \?\? null\)/);
    expect(fileHook).toMatch(/inFlightPages\(segment, task\.current, task\.total\)/);
    expect(fileHook).toMatch(/setTaskStartedAt\(stageStartedAtFromPerformance\(task\.performance \?\? null, 'recognition'\)\)/);
    // hook 层（use-playground）：return 对象透传五字段
    expect(playgroundHook).toMatch(/taskSegment: fileCtx\.taskSegment,/);
    expect(playgroundHook).toMatch(/taskInFlight: fileCtx\.taskInFlight,/);
    expect(playgroundHook).toMatch(/taskStartedAt: fileCtx\.taskStartedAt,/);
    expect(playgroundHook).toMatch(/taskTextMode: fileCtx\.taskTextMode,/);
    expect(playgroundHook).toMatch(/redactProgress,/);
    // context 层：类型 + 映射 + 依赖数组（五字段缺一即测试红——防止三槽拆分漏搬）
    for (const field of ['taskSegment', 'taskInFlight', 'taskStartedAt', 'taskTextMode', 'redactProgress'] as const) {
      expect(context).toContain(`${field}: PlaygroundContextValue['${field}'];`);
      expect(context).toMatch(new RegExp(`${field}: ctx\\.${field},`));
      expect(context).toMatch(new RegExp(`ctx\\.${field},`));
    }
    // 页面层：loading 拿到四字段 + 打码小卡渲染 redactProgress
    expect(page).toMatch(/segment=\{taskRecognition \? taskSegment : null\}/);
    expect(page).toMatch(/inFlight=\{taskRecognition \? taskInFlight : null\}/);
    expect(page).toMatch(/startedAt=\{taskRecognition \? taskStartedAt : null\}/);
    expect(page).toMatch(/textMode=\{taskRecognition \? taskTextMode : false\}/);
    expect(page).toMatch(/\{redactProgress && \(/);
    expect(page).toMatch(/variant="redaction"/);
  });
});

// Issue#327（门⑥ 返工）：子集预览（识别在飞的「先匿名化已处理页」）的三处接线钉。
// 评审 C-3/C-4 实证的两类缺陷都属于**表达式级**问题（tsc/单测都抓不住）：
// - C-3：handleDownload 的 URL 里读 subsetPreview，但依赖数组漏了它 ⇒ 恒发全卷 URL；
// - C-4：结果页把「子集序号」直接当原卷页号发给 page-image ⇒ 非前缀子集永久 404；
// - 上下文白名单漏挂 subsetPreview ⇒ 页面层拿到的永远是 undefined（入口永不出现）。
describe('Issue#327 子集预览接线契约：三处接线不得被静默摘掉', () => {
  it('上下文投影白名单必须包含子集三字段（漏挂 ⇒ 页面层恒 undefined）', () => {
    for (const field of [
      'subsetPreview',
      'subsetReadyCount',
      'subsetTotalPages',
      'subsetPages',
    ] as const) {
      expect(context).toContain(`${field}: PlaygroundContextValue['${field}'];`);
      expect(context).toMatch(new RegExp(`${field}: ctx\\.${field},`));
      expect(context).toMatch(new RegExp(`ctx\\.${field},`));
    }
  });

  // 2026-11-16 追加：识别中「先选页」与「编辑页导出带手拉框」的接线钉。
  // 这两条都是**表达式级**问题——漏传 prop 时 tsc/单测全绿，只有真跑才炸
  // （本轮实测：上下文白名单漏 subsetPages ⇒ 选页弹窗一打开就
  //  `TypeError: Cannot read properties of undefined (reading 'length')`）。
  it('2026-11-16：选页弹窗吃 subsetPages、导出弹窗吃 boundingBoxes、浮窗紧凑页格已删', () => {
    expect(page).toMatch(/<PlaygroundSubsetPicker[\s\S]*?pages=\{subsetPages\}/);
    expect(page).toMatch(/<PlaygroundExportDialog[\s\S]*?boundingBoxes=\{boundingBoxes\}/);
    // 识别期右下角浮窗里的紧凑页格（PlaygroundPageExport compact）必须删干净
    expect(page).not.toMatch(/<PlaygroundPageExport/);
    expect(pageExport).not.toMatch(/compact/);
  });

  it('下载/翻页：handleDownload 走唯一 URL 实现 + 依赖含 subsetPreview（C-3）；结果页把子集序号翻回原卷页号（C-4）', () => {
    // C-3 ①：URL 构造收敛到 fileApi.getDownloadUrl 一份实现，生产侧不得再手写
    // （二轮评审：api.ts 加了 preview 形参却仍手写 URL ⇒ 两份实现；旧判据 `/preview=true/`
    //  被注释里的同名字面量满足，属假保证，本轮改掉）
    expect(playgroundHook).toMatch(
      /fileApi\.getDownloadUrl\(fileCtx\.fileInfo\.file_id, true, !!subsetPreview\)/,
    );
    expect(playgroundHook).not.toMatch(/download\?redacted=true/);
    // C-3 ②：读 subsetPreview 的同一表达式必须在依赖数组里（漏了 ⇒ 恒发全卷 URL）
    expect(playgroundHook).toMatch(/\[fileCtx\.fileInfo, subsetPreview\]/);
    // C-4：子集序号 → 原卷页号的唯一换算函数（lib/subset-pages.ts）必须被结果页接线
    expect(page).toMatch(/previewPageMap=\{subsetPreview\?\.page_map \?\? null\}/);
    // 三轮必修2：子集回执不落盘 ⇒ 草稿阶段降级（否则刷新后结果页退回全卷口径）
    expect(playgroundHook).toMatch(/draftStageForResume\(fileCtx\.stage, subsetPreview !== null\)/);
  });

  it('识别终态不得静默降级为子集（I-1 红线：只有识别在飞才允许子集口径）', () => {
    expect(playgroundHook).toMatch(/import \{[^}]*isRecognitionInFlight[^}]*\} from '@\/features\/playground\/lib\/subset-pages';/);
    expect(playgroundHook).toMatch(/isRecognitionInFlight\(pageStatus\.recognition_state\)/);
  });
});
