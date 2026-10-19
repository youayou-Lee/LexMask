// Copyright 2026 LexMask Contributors

import { useState, useCallback, useEffect, useRef } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { ApiError, authFetch } from '@/services/api-client';
import { cancelJob } from '@/services/jobsApi';
import { resolveAwaitingReviewTask, submitRecognitionTask } from '@/services/singleTaskApi';
import { useSingleTask } from '@/features/single-task/single-task-context';
import { SINGLE_TASK_POLL_MS } from '@/constants/timing';
import { showToast } from '@/components/Toast';
import { t } from '@/i18n';
import { localizeErrorMessage } from '@/utils/localizeError';
import { ACCEPTED_UPLOAD_FILE_TYPES } from '@/utils/fileUploadAccept';
import { safeJson } from '../utils';
import { boundingBoxesFromServerInfo, planServerCachedResume } from '../lib/playground-draft';
import { recognitionStageFromMessage } from '../lib/task-progress';
import { shouldEnterPreviewDuringRecognition } from '../lib/progressive-render';
import type {
  FileInfo,
  Entity,
  BoundingBox,
  Stage,
  UploadResponse,
  ParseResponse,
} from '../types';

/** 服务端识别缓存实体 → 前端 Entity（loadExistingFile 缓存分支与任务迁移共用同一映射）。 */
function toEntitiesWithDefaults(entities: Record<string, unknown>[]): Entity[] {
  return entities.map((e, idx) =>
    ({
      ...e,
      id: (e.id as string | undefined) || `entity_${idx}`,
      selected: (e.selected as boolean | undefined) ?? true,
      source: (e.source as Entity['source'] | undefined) || 'llm',
    }) as Entity,
  );
}

async function responseErrorMessage(res: Response, fallbackKey: string) {
  try {
    const data = await safeJson<{ detail?: unknown; message?: unknown; error?: unknown }>(res);
    const detail = data.detail ?? data.message ?? data.error;
    if (typeof detail === 'string' && detail.trim()) return detail;
  } catch {
    // Keep the localized fallback when the response body is not JSON.
  }
  return t(fallbackKey);
}

/** 读取后端错误信封（响应体只能读一次，务必一次取全 code + message，评审 P1-5）。 */
async function readErrorEnvelope(
  res: Response,
): Promise<{ code: string | null; message: string | null }> {
  try {
    const data = await safeJson<{
      error_code?: unknown;
      detail?: unknown;
      message?: unknown;
      error?: unknown;
    }>(res);
    const code =
      typeof data?.error_code === 'string' && data.error_code ? data.error_code : null;
    const detail = data?.detail ?? data?.message ?? data?.error;
    const message = typeof detail === 'string' && detail.trim() ? detail.trim() : null;
    return { code, message };
  } catch {
    return { code: null, message: null };
  }
}

/** Issue #30：待输入密码的加密 PDF。 */
export interface EncryptedPdfPrompt {
  fileId: string;
  filename: string;
  /** 原始文件字节数：解密成功续跑时回填 fileInfo，避免草稿快照尺寸显示 0（Issue #32 P2-6）。 */
  fileSize?: number;
}

export interface PendingFile {
  fileId: string;
  fileType: string;
  isScanned: boolean;
  pageCount: number;
  content: string;
}

export interface UsePlaygroundFileOptions {
  /** Ref to latest selectedOcrHasTypes for use in async callbacks */
  latestOcrHasTypesRef: React.RefObject<string[]>;
  /** Ref to latest selectedVisualFeatureTypes for use in async callbacks */
  latestVisualFeatureTypesRef: React.RefObject<string[]>;
  /** Ref to latest selectedTypes for use in async callbacks */
  latestSelectedTypesRef: React.RefObject<string[]>;
  /** Reset entity history */
  resetEntityHistory: () => void;
  /** Reset image history */
  resetImageHistory: () => void;
  /** Set entities from recognition result */
  setEntities: React.Dispatch<React.SetStateAction<Entity[]>>;
  /** Restore the entity-type selection that produced the cached result (server resume) */
  setSelectedTypes: (ids: string[]) => void;
  /** Set bounding boxes from recognition result */
  setBoundingBoxes: React.Dispatch<React.SetStateAction<BoundingBox[]>>;
  /** Return a user-facing reason when automatic recognition should not run yet */
  getRecognitionBlocker?: (file: PendingFile) => string | null;
}

export function usePlaygroundFile(options: UsePlaygroundFileOptions) {
  const optionsRef = useRef(options);
  optionsRef.current = options;

  const [stage, setStage] = useState<Stage>('upload');
  const [content, setContent] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState('');
  /**
   * Issue#123：识别期间已完成的页数与总页数（仅多页流式识别时非空）。
   * 供表现层判断是否把全屏遮罩降级为 passive（透明、不遮挡），让逐页回流的框在识别期间可见。
   * 多页扫描件之外（文本 NER / 图片单页 / 非流式路径）一律保持 null
   * ⇒ 遮罩行为与改前逐字节一致。
   */
  const [visionStream, setVisionStream] = useState<{ completed: number; total: number } | null>(
    null,
  );
  const [uploadIssue, setUploadIssue] = useState<string | null>(null);
  const [recognitionIssue, setRecognitionIssue] = useState<string | null>(null);
  // Issue #30：解析返回 PDF_ENCRYPTED_NEEDS_PASSWORD 时挂起流程，弹密码框
  const [encryptedPrompt, setEncryptedPrompt] = useState<EncryptedPdfPrompt | null>(null);

  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null);

  // --- Issue#194：识别改走后台单文件任务（提交任务 + 听进度） ---
  // 全局任务账本（SingleTaskProvider 在 Layout 层挂载）：watch 注册、轮询推进、taskForFile 查询。
  const { taskForFile, watch, refresh } = useSingleTask();
  /**
   * 当前会话里驱动识别的任务（null = 识别不由任务驱动）。
   * 非 null 期间：遮罩一律 passive（画布/文本已就位，前端没有可遮挡的「正在进行」画面）、
   * loadingMessage/visionStream 由 provider 轮询结果映射（见下方订阅 effect）。
   */
  const [taskRecognition, setTaskRecognition] = useState<{ jobId: string } | null>(null);
  const taskRecognitionRef = useRef<{ jobId: string } | null>(null);
  const setTaskRecognitionJob = useCallback((value: { jobId: string } | null) => {
    taskRecognitionRef.current = value;
    setTaskRecognition(value);
  }, []);
  /**
   * Issue#194 Task 11：识别中任务的 item performance（provider 轮询经 SingleTaskView 透出）。
   * 进行中视图（PlaygroundLoading）据此用与结果页详情卡同一个 buildProcessingRows
   * 渲染已产出阶段行；任务退出（迁移/失败/取消）即清空。
   */
  const [taskPerformance, setTaskPerformance] = useState<Record<string, unknown> | null>(null);
  /**
   * Issue#194 用户验收③④：当前段人话段名（progress_message 经 recognitionStageFromMessage
   * 映射 + t）。等待视图的段名行（唯一活数字）与 ✓ 清单的当前段排除都用它；任务退出即清空。
   */
  const [taskStageLabel, setTaskStageLabel] = useState<string | null>(null);
  /** 迁移处理守卫：每个 job 的 awaiting_review/终态处理只执行一次（防重复拉快照/重复报错）。 */
  const handledJobRef = useRef<string | null>(null);
  /** 用户主动取消过的 job：provider 报 failed 时静默（cancelProcessing 已收拾过 UI）。 */
  const cancelledJobsRef = useRef<Set<string>>(new Set());
  /** reattach 重挂后 provider 迟迟不认领（job 已被终态清除）的兜底退出定时器。 */
  const reattachGraceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  // fileInfo 的镜像 ref：异步回调里判断「这仍是当前文件的会话吗」（跨文件串染守卫），
  // 并给提交失败路径回填 filename/fileSize。所有写入口都走包装 setter，保证 ref 同步。
  const [fileInfo, setFileInfoRaw] = useState<FileInfo | null>(null);
  const fileInfoRef = useRef<FileInfo | null>(null);
  const setFileInfo = useCallback((info: FileInfo | null) => {
    fileInfoRef.current = info;
    setFileInfoRaw(info);
  }, []);
  const taskForFileRef = useRef(taskForFile);
  taskForFileRef.current = taskForFile;

  const isImageMode = !!fileInfo && (fileInfo.file_type === 'image' || !!fileInfo.is_scanned);

  // --- Cleanup abort on unmount ---
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      if (reattachGraceTimerRef.current) clearTimeout(reattachGraceTimerRef.current);
    };
  }, []);

  const cancelProcessing = useCallback(
    (notify = true) => {
      // Issue#194：任务驱动的识别 → 取消改调 cancelJob（后端标 cancelled ⇒ provider 报
      // failed；订阅 effect 对 cancelledJobsRef 里的 job 静默，不重复弹错）。
      const active = taskRecognitionRef.current;
      if (active) {
        cancelledJobsRef.current.add(active.jobId);
        handledJobRef.current = active.jobId;
        setTaskRecognitionJob(null);
        void cancelJob(active.jobId).catch(() => {
          // 取消失败不打断前端收拾：任务若仍推进到 awaiting_review，快照回填照常工作
        });
      }
      abortRef.current?.abort();
      abortRef.current = null;
      setPendingFile(null);
      setIsLoading(false);
      setLoadingMessage('');
      setRecognitionIssue(null);
      if (notify) {
        showToast(t('playground.cancelled'), 'info');
      }
    },
    [setTaskRecognitionJob],
  );

  const rejectionMessage = useCallback((rejection: FileRejection): string => {
    const firstError = rejection.errors[0];
    if (firstError?.code === 'file-invalid-type') {
      return t('playground.upload.rejectInvalidType').replace('{filename}', rejection.file.name);
    }
    if (firstError?.code === 'too-many-files') {
      return t('playground.upload.rejectTooMany');
    }
    return firstError?.message || t('playground.upload.rejectGeneric');
  }, []);

  const onDropRejected = useCallback(
    (rejections: FileRejection[]) => {
      const message = rejections[0]
        ? rejectionMessage(rejections[0])
        : t('playground.upload.rejectGeneric');
      setUploadIssue(message);
      showToast(message, 'error');
    },
    [rejectionMessage],
  );

  /** parse 成功后的公共落位：写 fileInfo/pendingFile，触发自动识别 effect。 */
  const applyParsedFile = useCallback(
    (fileId: string, filename: string, fileSize: number, parseData: ParseResponse) => {
      const isScanned = parseData.is_scanned || false;
      const pageCount = Math.max(1, Number(parseData.page_count || 1));
      const parsedFileType = parseData.file_type || 'pdf';
      const parsedContent = parseData.content || '';
      const parsedPages = Array.isArray(parseData.pages) ? parseData.pages : undefined;

      setFileInfo({
        file_id: fileId,
        filename,
        file_size: fileSize,
        file_type: parsedFileType,
        is_scanned: isScanned,
        page_count: pageCount,
        pages: parsedPages,
      });
      setContent(parsedContent);
      const opts = optionsRef.current;
      opts.setBoundingBoxes([]);
      opts.resetImageHistory();
      opts.setEntities([]);

      setPendingFile({
        fileId,
        fileType: parsedFileType,
        isScanned,
        pageCount,
        content: parsedContent,
      });

      // Issue#123（①.5 冷审 C1 修正）：多页扫描件/图片在 parse 完成后**立即**进入 preview。
      // 改前 stage 直到识别成功才切 preview（本文件 doRecognition 末尾），整卷识别期间
      // 画布根本不在渲染树里 ⇒ 逐页回流的框无处可显，「首屏=全卷」。识别期间 preview
      // 已挂载、动作按钮由 isLoading 禁用（playground-page.tsx:479/:514），
      // 框按页 append（onPageComplete），遮罩由 passive 降级为不遮挡。
      if (
        shouldEnterPreviewDuringRecognition({
          pageCount,
          isScanned,
          fileType: parsedFileType,
        })
      ) {
        setStage('preview');
      }
    },
    [setFileInfo],
  );

  // --- File upload ---
  const handleFileDrop = useCallback(async (acceptedFiles: File[]) => {
    if (acceptedFiles.length === 0) return;
    const file = acceptedFiles[0];
    // Issue#194：换文件前取消本会话在途任务——单文件任务守门是账号级的，
    // 不取消的话新提交会对自己的上一个任务吃 409。
    const activeTask = taskRecognitionRef.current;
    if (activeTask) {
      cancelledJobsRef.current.add(activeTask.jobId);
      handledJobRef.current = activeTask.jobId;
      setTaskRecognitionJob(null);
      void cancelJob(activeTask.jobId).catch(() => {});
    }
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;

    setIsLoading(true);
    setStage('upload');
    setUploadIssue(null);
    setRecognitionIssue(null);

    try {
      setLoadingMessage(t('playground.uploading'));
      const formData = new FormData();
      formData.append('file', file);
      formData.append('upload_source', 'playground');

      const uploadRes = await authFetch('/api/v1/files/upload', {
        method: 'POST',
        body: formData,
        signal,
      });
      if (signal.aborted) return;
      if (!uploadRes.ok) {
        throw new Error(await responseErrorMessage(uploadRes, 'playground.uploadFailed'));
      }
      const uploadData = await safeJson<UploadResponse>(uploadRes);
      if (signal.aborted) return;

      setLoadingMessage(t('playground.parsing'));
      const parseRes = await authFetch(`/api/v1/files/${uploadData.file_id}/parse`, { signal });
      if (signal.aborted) return;
      if (!parseRes.ok) {
        const { code, message } = await readErrorEnvelope(parseRes);
        // Issue #30：需打开密码的 PDF 不报错，挂起流程弹密码框
        if (code === 'PDF_ENCRYPTED_NEEDS_PASSWORD') {
          setEncryptedPrompt({ fileId: uploadData.file_id, filename: uploadData.filename, fileSize: file.size });
          setIsLoading(false);
          setLoadingMessage('');
          return;
        }
        throw new Error(message || t('playground.parseFailed'));
      }
      const parseData = await safeJson<ParseResponse>(parseRes);
      if (signal.aborted) return;

      applyParsedFile(uploadData.file_id, uploadData.filename, uploadData.file_size, parseData);
    } catch (err) {
      if (signal.aborted) return;
      showToast(localizeErrorMessage(err, 'playground.processFailed'), 'error');
      setIsLoading(false);
      setLoadingMessage('');
    } finally {
      if (abortRef.current === controller) {
        abortRef.current = null;
      }
    }
  }, [applyParsedFile, setTaskRecognitionJob]);

  /** 活跃识别任务重挂：取 info+parse 重建会话现场后挂回 provider，等任务推进，不自动识别。 */
  const adoptActiveTask = useCallback(
    async (fileId: string, jobId: string, signal: AbortSignal) => {
      const opts = optionsRef.current;
      setLoadingMessage(t('playground.parsing'));
      const [infoRes, parseRes] = await Promise.all([
        authFetch(`/api/v1/files/${fileId}`, { signal }),
        authFetch(`/api/v1/files/${fileId}/parse`, { signal }),
      ]);
      if (signal.aborted) return;
      if (!infoRes.ok) throw new Error(await responseErrorMessage(infoRes, 'playground.parseFailed'));
      const info = await safeJson<Record<string, unknown>>(infoRes);
      if (!parseRes.ok) throw new Error(t('playground.parseFailed'));
      const parseData = await safeJson<ParseResponse>(parseRes);
      if (signal.aborted) return;

      const isScanned = parseData.is_scanned || false;
      const pageCount = Math.max(1, Number(parseData.page_count || 1));
      const parsedFileType = parseData.file_type || 'pdf';
      const filename = (info.original_filename as string | undefined) || fileId;

      setFileInfo({
        file_id: fileId,
        filename,
        file_size: (info.file_size as number | undefined) || 0,
        file_type: parsedFileType,
        is_scanned: isScanned,
        page_count: pageCount,
        pages: Array.isArray(parseData.pages) ? parseData.pages : undefined,
      });
      setContent(parseData.content || '');
      opts.setBoundingBoxes([]);
      opts.resetImageHistory();
      opts.setEntities([]);
      if (
        shouldEnterPreviewDuringRecognition({
          pageCount,
          isScanned,
          fileType: parsedFileType,
        })
      ) {
        setStage('preview');
      }

      watch(jobId, fileId, filename);
      cancelledJobsRef.current.delete(jobId);
      handledJobRef.current = null;
      setTaskRecognitionJob({ jobId });
      // isLoading 保持 true：识别中视图（passive 遮罩 + 任务进度）由订阅 effect 接管，
      // awaiting_review 迁移 / failed / 用户取消时才退出。
    },
    [setTaskRecognitionJob, setFileInfo, watch],
  );

  // 从服务端按 file_id 重建会话（历史页「回到现场」且无草稿时的 R2 路径）：
  // 后端识别成功后会把 entities + 当时的识别配置写进文件记录，先取缓存直接
  // 恢复「当时的现场」；从未识别过（实体为空，含扫描件）才 parse + 重新识别。
  const loadExistingFile = useCallback(
    async (fileId: string) => {
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const { signal } = controller;

      setIsLoading(true);
      setStage('upload');
      setUploadIssue(null);
      setRecognitionIssue(null);

      const opts = optionsRef.current;
      try {
        // Issue#194（T8 交接约束）：provider 的挂载恢复是异步的（恢复条目的 fileId
        // 要等首轮轮询回填），判断「有无活跃任务」必须容忍一拍 miss——
        // 先问一次，miss 则 await refresh() 后再问一次，仍 miss 才走旧路径。
        let activeTask = taskForFile(fileId);
        if (!activeTask) {
          await refresh();
          if (signal.aborted) return;
          activeTask = taskForFile(fileId);
        }
        if (activeTask && activeTask.phase === 'recognizing') {
          // 活跃识别任务：进入「识别中视图」等 provider 推进，不再走 rerun 自动识别
          await adoptActiveTask(fileId, activeTask.jobId, signal);
          return;
        }
        if (activeTask) {
          // 非识别中的既有任务（awaiting_review/redacting/done/failed）：走旧路径，
          // 但登记该 job 的迁移已处理——awaiting_review 的实体由下方服务端缓存
          // 分支恢复，避免订阅 effect 再拉一次快照覆盖。
          handledJobRef.current = activeTask.jobId;
        }

        setLoadingMessage(t('playground.parsing'));
        const [infoRes, parseRes] = await Promise.all([
          authFetch(`/api/v1/files/${fileId}`, { signal }),
          authFetch(`/api/v1/files/${fileId}/parse`, { signal }),
        ]);
        if (signal.aborted) return;
        if (!infoRes.ok) throw new Error(await responseErrorMessage(infoRes, 'playground.parseFailed'));
        const info = await safeJson<Record<string, unknown>>(infoRes);
        if (!parseRes.ok) {
          const { code, message } = await readErrorEnvelope(parseRes);
          // Issue #30：历史会话里的加密 PDF 同样挂起弹密码框
          if (code === 'PDF_ENCRYPTED_NEEDS_PASSWORD') {
            setEncryptedPrompt({
              fileId,
              filename: (info.original_filename as string | undefined) || fileId,
              fileSize: (info.file_size as number | undefined) || undefined,
            });
            setIsLoading(false);
            setLoadingMessage('');
            return;
          }
          throw new Error(message || t('playground.parseFailed'));
        }
        const parseData = await safeJson<ParseResponse>(parseRes);
        if (signal.aborted) return;

        const isScanned = parseData.is_scanned || false;
        const pageCount = Math.max(1, Number(parseData.page_count || 1));
        const parsedFileType = parseData.file_type || 'pdf';
        const parsedContent = parseData.content || '';
        const parsedPages = Array.isArray(parseData.pages) ? parseData.pages : undefined;

        setFileInfo({
          file_id: fileId,
          filename: (info.original_filename as string | undefined) || fileId,
          file_size: (info.file_size as number | undefined) || 0,
          file_type: parsedFileType,
          is_scanned: isScanned,
          page_count: pageCount,
          pages: parsedPages,
        });
        setContent(parsedContent);
        opts.setBoundingBoxes([]);
        opts.resetImageHistory();

        const cached = planServerCachedResume(info);
        if (cached.mode === 'cached') {
          // 命中服务端识别缓存：恢复当时的实体与识别项配置，跳过重新识别
          opts.setEntities(toEntitiesWithDefaults(cached.entities));
          opts.resetEntityHistory();
          if (cached.entityTypeIds) opts.setSelectedTypes(cached.entityTypeIds);
          setStage('preview');
          setIsLoading(false);
          setLoadingMessage('');
          showToast(t('playground.restoredFromServer'), 'info');
          return;
        }

        // Issue#194 终审 A：awaiting_review 任务不在 provider 恢复的 active 清单里
        // （上面的活跃任务分支必 miss），扫描件的实体又恒空（cached 分支必 miss）⇒
        // 直接落 rerun 会自动重提第二个识别任务（旧任务孤儿）。回场先按 fileId 直查
        // awaiting_review 清单：命中即恢复确认视图、不自动重提——后续打码提交由
        // handleRedact 自带的 awaiting_review 兜底解析接手，实体列表允许为空
        // （扫描件的实体在 vision 框/确认流呈现，沿用确认页对空实体的既有表现）。
        let awaitingTask: Awaited<ReturnType<typeof resolveAwaitingReviewTask>> = null;
        try {
          awaitingTask = await resolveAwaitingReviewTask(fileId);
        } catch {
          // 查询失败（离线等）不阻塞回场：下方重提路径自会暴露网络问题
          awaitingTask = null;
        }
        if (signal.aborted) return;
        if (awaitingTask) {
          opts.setEntities([]);
          // Issue#194 用户验收①：回场恢复确认视图同样要装回快照里的框（与上方
          // 订阅 effect 的 awaiting_review 迁移同一语义，两条路都到确认视图）
          if (isScanned || parsedFileType === 'image') {
            opts.setBoundingBoxes(boundingBoxesFromServerInfo(info.bounding_boxes));
          }
          setStage('preview');
          setIsLoading(false);
          setLoadingMessage('');
          showToast(t('playground.restoredAwaitingReview'), 'info');
          return;
        }

        opts.setEntities([]);
        setPendingFile({
          fileId,
          fileType: parsedFileType,
          isScanned,
          pageCount,
          content: parsedContent,
        });
      } catch (err) {
        if (signal.aborted) return;
        showToast(localizeErrorMessage(err, 'playground.restoreFileGone'), 'error');
        setIsLoading(false);
        setLoadingMessage('');
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
      }
    },
    [adoptActiveTask, refresh, taskForFile, setFileInfo],
  );

  // --- Auto-recognition after upload（Issue#194：改提交后台任务） ---
  // 内部约定（T9）：识别流 = submitRecognitionTask(fileId, selectedTypes) → watch(jobId,…) →
  // provider 轮询推进 phase → 下方订阅效果把 recognizing 的进度映射成 loadingMessage/visionStream、
  // 把 awaiting_review 迁移成「拉 GET /files/{id} 快照 → applyEntities → setStage('preview')」
  // （planServerCachedResume cached 分支同款映射）。本组件不再直调旧的文本/逐页图像识别
  // 端点与 utils 里的逐页检测函数——它们保留给重跑等既有调用方（AC5 红线，见 wiring 契约）。
  useEffect(() => {
    if (!pendingFile) return;
    const { fileId } = pendingFile;
    setPendingFile(null);

    const opts = optionsRef.current;

    const blocker = opts.getRecognitionBlocker?.(pendingFile) ?? null;
    if (blocker) {
      setRecognitionIssue(blocker);
      setStage('preview');
      setIsLoading(false);
      setLoadingMessage('');
      showToast(blocker, 'info');
      return;
    }

    const startTask = async () => {
      setRecognitionIssue(null);
      setVisionStream(null);
      setIsLoading(true);
      setLoadingMessage(t('playground.taskParsing'));
      try {
        // Issue#194 终审 B：扫描件/图片的视觉要素选择随任务透传（写进 job config，
        // 识别工人视觉段按 ocr_has_types/visual_feature_types 读取）——不传则编排器
        // 恒按默认全类型集跑，用户勾选对任务化扫描件无效（老同步流的回归）。
        const isVisionFile = pendingFile.isScanned || pendingFile.fileType === 'image';
        const ref = await submitRecognitionTask(
          fileId,
          opts.latestSelectedTypesRef.current,
          isVisionFile
            ? {
                ocrHasTypes: opts.latestOcrHasTypesRef.current,
                visualFeatureTypes: opts.latestVisualFeatureTypesRef.current,
              }
            : undefined,
        );
        if (fileInfoRef.current?.file_id !== fileId) return; // 提交期间用户已切走/重置
        watch(ref.job_id, fileId, fileInfoRef.current?.filename || fileId);
        cancelledJobsRef.current.delete(ref.job_id);
        handledJobRef.current = null;
        setTaskRecognitionJob({ jobId: ref.job_id });
        // 识别中视图自此由订阅 effect 驱动（isLoading 保持 true）
      } catch (err) {
        if (fileInfoRef.current?.file_id !== fileId) return;
        if (err instanceof ApiError && err.errorCode === 'PDF_ENCRYPTED_NEEDS_PASSWORD') {
          // Issue #30 既有机制：挂起流程弹密码框
          setEncryptedPrompt({
            fileId,
            filename: fileInfoRef.current?.filename || fileId,
            fileSize: fileInfoRef.current?.file_size || undefined,
          });
          setIsLoading(false);
          setLoadingMessage('');
          return;
        }
        // 409（账号下已有活动任务）等提交失败：recognitionIssue 展示后端文案，不切 stage
        const message = localizeErrorMessage(err, 'playground.recognizeFailed');
        setRecognitionIssue(message);
        showToast(message, 'error');
        setIsLoading(false);
        setLoadingMessage('');
      }
    };

    void startTask();
  }, [pendingFile, setTaskRecognitionJob, watch]);

  // --- Issue#194：任务订阅——进度映射 + awaiting_review 迁移 + 失败/兜底退出 ---
  useEffect(() => {
    const active = taskRecognition;
    if (!active) return;
    const fileId = fileInfoRef.current?.file_id;
    if (!fileId) return;
    const task = taskForFile(fileId);
    if (!task || task.jobId !== active.jobId) return;

    if (task.phase === 'recognizing') {
      // 后端进度语义（task_queue_pipelines）：parse 段无计数（total=0）→「后台解析中」；
      // ner 段 1/1、vision 段 page/total →「后台识别中 current/total」+ visionStream 真进度
      //（沿用 Issue#123 的 UI 管道，字段换源）。
      setTaskPerformance(task.performance ?? null);
      if (task.total > 0) {
        setVisionStream({ completed: task.current, total: task.total });
        // 用户验收③④：段名（人话）单独透出给等待视图的段名行（第二段不像重新开始）；
        // loadingMessage 升级为「正在识别「文件名」」顶部行——等待页从此只有一处活数字。
        // 拿不到文件名回落既有文案（段名+页码 / 后台识别中模板）。
        const stage = recognitionStageFromMessage(task.message);
        setTaskStageLabel(stage ? t(stage.labelKey) : null);
        const filename = fileInfoRef.current?.filename;
        if (filename) {
          setLoadingMessage(t('playground.identifying').replace('{filename}', filename));
        } else if (stage) {
          setLoadingMessage(`${t(stage.labelKey)} ${stage.current}/${stage.total}`);
        } else {
          setLoadingMessage(
            t('playground.taskRecognizing')
              .replace('{current}', String(task.current))
              .replace('{total}', String(task.total)),
          );
        }
      } else {
        setVisionStream(null);
        setTaskStageLabel(null);
        setLoadingMessage(t('playground.taskParsing'));
      }
      return;
    }

    if (task.phase === 'awaiting_review') {
      // 识别完成：拉文件记录快照回填实体（迁移只执行一次——handledJobRef 守卫）
      if (handledJobRef.current === task.jobId) return;
      handledJobRef.current = task.jobId;
      const jobId = task.jobId;
      const controller = new AbortController();
      abortRef.current = controller;
      const { signal } = controller;
      void (async () => {
        try {
          const res = await authFetch(`/api/v1/files/${fileId}`, { signal });
          if (signal.aborted || fileInfoRef.current?.file_id !== fileId) return;
          if (res.ok) {
            const info = await safeJson<Record<string, unknown>>(res);
            if (signal.aborted || fileInfoRef.current?.file_id !== fileId) return;
            const cached = planServerCachedResume(info);
            const opts = optionsRef.current;
            if (cached.mode === 'cached') {
              opts.setEntities(toEntitiesWithDefaults(cached.entities));
              opts.resetEntityHistory();
              if (cached.entityTypeIds) opts.setSelectedTypes(cached.entityTypeIds);
            }
            // Issue#194 用户验收①：扫描件/图片的框只在快照里（任务流没有旧同步流的
            // 逐页 onPageComplete 回流）——确认视图必须把 bounding_boxes 装进画布，
            // 否则服务端明明有几百个框、用户看到的却是空画布。文本件不受影响。
            const currentInfo = fileInfoRef.current;
            if (currentInfo && (currentInfo.is_scanned || currentInfo.file_type === 'image')) {
              opts.setBoundingBoxes(boundingBoxesFromServerInfo(info.bounding_boxes));
            }
          }
          setStage('preview');
        } catch {
          // 快照拉取失败不阻塞：实体可稍后经历史页/服务端缓存再取，识别确已完成
          if (!signal.aborted) setStage('preview');
        } finally {
          if (!signal.aborted && handledJobRef.current === jobId) {
            setTaskRecognitionJob(null);
            setTaskPerformance(null);
            setTaskStageLabel(null);
            setVisionStream(null);
            setIsLoading(false);
            setLoadingMessage('');
          }
        }
      })();
      return;
    }

    if (task.phase === 'redacting' || task.phase === 'done') {
      // 打码段/已完成：识别早已结束（草稿恢复重挂到陈旧 job 的兜底），退出识别中视图即可
      if (handledJobRef.current === task.jobId) return;
      handledJobRef.current = task.jobId;
      setTaskRecognitionJob(null);
      setTaskPerformance(null);
      setTaskStageLabel(null);
      setVisionStream(null);
      setIsLoading(false);
      setLoadingMessage('');
      return;
    }

    // failed：cancelled 视同 failed（provider 语义）——用户主动取消的静默退出
    if (handledJobRef.current === task.jobId) return;
    handledJobRef.current = task.jobId;
    setTaskRecognitionJob(null);
    setTaskPerformance(null);
    setTaskStageLabel(null);
    setVisionStream(null);
    if (cancelledJobsRef.current.has(task.jobId)) return;
    setIsLoading(false);
    setLoadingMessage('');
    setStage('preview');
    const message = task.error || t('playground.recognizeFailed');
    setRecognitionIssue(message);
    showToast(message, 'error');
  }, [taskForFile, taskRecognition, setTaskRecognitionJob]);

  /**
   * 草稿恢复（applyDraftSnapshot）带 taskJobId 时重挂任务：watch 幂等注册 + 等 provider 推进。
   * job 已被终态清除（账本里查无此 job）时由宽限定时器兜底退出识别中视图。
   */
  const reattachTask = useCallback(
    (jobId: string, fileId: string, filename: string) => {
      if (reattachGraceTimerRef.current) clearTimeout(reattachGraceTimerRef.current);
      watch(jobId, fileId, filename);
      cancelledJobsRef.current.delete(jobId);
      handledJobRef.current = null;
      setTaskRecognitionJob({ jobId });
      setTaskPerformance(null);
      setTaskStageLabel(null); // 旧任务残留清空，等 provider 轮询回填
      setIsLoading(true);
      setRecognitionIssue(null);
      setLoadingMessage(t('playground.taskParsing'));
      reattachGraceTimerRef.current = setTimeout(() => {
        if (taskRecognitionRef.current?.jobId !== jobId) return;
        if (taskForFileRef.current(fileId)) return; // provider 已认领，继续等轮询
        handledJobRef.current = jobId;
        setTaskRecognitionJob(null);
        setTaskPerformance(null);
        setTaskStageLabel(null);
        setVisionStream(null);
        setIsLoading(false);
        setLoadingMessage('');
      }, SINGLE_TASK_POLL_MS * 3);
    },
    [setTaskRecognitionJob, watch],
  );

  // --- Issue #30：密码解密成功后重跑 parse → 自动识别 ---
  const handleDecrypted = useCallback(
    async (fileId: string, filename: string, fileSize?: number) => {
      setEncryptedPrompt(null);
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;
      const { signal } = controller;

      setIsLoading(true);
      setStage('upload');
      setUploadIssue(null);
      setRecognitionIssue(null);
      try {
        setLoadingMessage(t('playground.parsing'));
        const parseRes = await authFetch(`/api/v1/files/${fileId}/parse`, { signal });
        if (signal.aborted) return;
        if (!parseRes.ok) {
          throw new Error(await responseErrorMessage(parseRes, 'playground.parseFailed'));
        }
        const parseData = await safeJson<ParseResponse>(parseRes);
        if (signal.aborted) return;
        applyParsedFile(fileId, filename, fileSize ?? 0, parseData);
      } catch (err) {
        if (signal.aborted) return;
        showToast(localizeErrorMessage(err, 'playground.processFailed'), 'error');
        setIsLoading(false);
        setLoadingMessage('');
      } finally {
        if (abortRef.current === controller) {
          abortRef.current = null;
        }
      }
    },
    [applyParsedFile],
  );

  const clearEncryptedPrompt = useCallback(() => {
    setEncryptedPrompt(null);
  }, []);

  // --- Dropzone ---
  const dropzone = useDropzone({
    onDrop: handleFileDrop,
    onDropRejected,
    accept: ACCEPTED_UPLOAD_FILE_TYPES,
    maxFiles: 1,
    disabled: isLoading,
    noClick: true,
  });

  return {
    stage,
    setStage,
    fileInfo,
    setFileInfo,
    content,
    setContent,
    isLoading,
    setIsLoading,
    loadingMessage,
    setLoadingMessage,
    visionStream,
    setVisionStream,
    cancelProcessing,
    handleFileDrop,
    loadExistingFile,
    uploadIssue,
    recognitionIssue,
    setRecognitionIssue,
    isImageMode,
    dropzone,
    encryptedPrompt,
    handleDecrypted,
    clearEncryptedPrompt,
    // Issue#194：任务驱动识别的状态与重挂入口（表现层据此降级遮罩 / 草稿恢复据此重挂）
    taskRecognition,
    taskPerformance,
    taskStageLabel,
    reattachTask,
  };
}
