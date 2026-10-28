// Copyright 2026 LexMask Contributors

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { showToast } from '@/components/Toast';
import { STORAGE_KEYS } from '@/constants/storage-keys';
import { useAuth } from '@/features/auth/auth-context';
import { t } from '@/i18n';
import { useServiceHealth, type ServicesHealth } from '@/hooks/use-service-health';
import {
  getScopedStorageItem,
  removeStorageItem,
  scopedStorageKey,
  setScopedStorageItem,
} from '@/lib/storage';
import { authFetch, downloadFile } from '@/services/api-client';
import { getJob } from '@/services/jobsApi';
import { resolveAwaitingReviewTask, submitRedactionTask } from '@/services/singleTaskApi';
import { useSingleTask } from '@/features/single-task/single-task-context';
import type { VersionHistoryEntry } from '@/types';
import { localizeErrorMessage } from '@/utils/localizeError';
import {
  buildDraftSnapshot,
  parseDraft,
  planResume,
  serializeDraft,
  type PlaygroundDraftSnapshot,
} from '../lib/playground-draft';
import {
  safeJson,
  buildPseudonymCsv,
  triggerDownload,
  boxesForRedactPayload,
  isVisualPreviewMode,
  locateEntityBoxes,
  mergeNerBoxes,
  syncEntitiesWithNerBoxes,
} from '../utils';
import { recognitionFailedPages } from '../lib/processing-details';
import { usePlaygroundEntities } from './use-playground-entities';
import { usePlaygroundFile } from './use-playground-file';
import { usePlaygroundHistory } from './use-playground-history';
import { usePlaygroundImage } from './use-playground-image';
import { usePlaygroundRecognition } from './use-playground-recognition';

type ServiceKey = keyof ServicesHealth['services'];

const BLOCKING_SERVICE_STATUSES = new Set(['offline', 'degraded']);

function isServiceBlocked(health: ServicesHealth | null, key: ServiceKey) {
  const status = health?.services[key]?.status;
  return typeof status === 'string' && BLOCKING_SERVICE_STATUSES.has(status);
}

function serviceLabel(health: ServicesHealth, key: ServiceKey) {
  const service = health.services[key];
  if (!service) return String(key);
  return `${t(`health.service.${key}`)}：${t(`health.${service.status}`)}`;
}

export function usePlayground() {
  const { status } = useAuth();
  const ownerKey =
    status?.authenticated && status.username ? status.username.toLowerCase() : 'anonymous';
  const recognition = usePlaygroundRecognition();
  const { health, checking: healthChecking } = useServiceHealth();
  const { setProcessingMode: setRecognitionProcessingMode } = recognition;

  // Issue#194：打码段复用识别段的同一个单文件任务（全局账本）。
  // provider 的挂载恢复是异步的（fileId 要等首轮轮询回填），查询经 ref 走最新值，
  // 提交前的一拍 miss 由 handleRedact 内 refresh 容忍（loadExistingFile 同款）。
  const { taskForFile: getSingleTask, watch: watchSingleTask, refresh: refreshSingleTasks } =
    useSingleTask();
  const getSingleTaskRef = useRef(getSingleTask);
  getSingleTaskRef.current = getSingleTask;
  const watchSingleTaskRef = useRef(watchSingleTask);
  watchSingleTaskRef.current = watchSingleTask;
  const refreshSingleTasksRef = useRef(refreshSingleTasks);
  refreshSingleTasksRef.current = refreshSingleTasks;

  const latestOcrHasTypesRef = useRef(recognition.selectedOcrHasTypes);
  const latestVisualFeatureTypesRef = useRef(recognition.selectedVisualFeatureTypes);
  const latestSelectedTypesRef = recognition.selectedTypesRef;
  latestOcrHasTypesRef.current = recognition.selectedOcrHasTypes;
  latestVisualFeatureTypesRef.current = recognition.selectedVisualFeatureTypes;

  const entityCtx = usePlaygroundEntities();

  const [redactionReport, setRedactionReport] = useState<Record<string, unknown> | null>(null);
  const [reportOpen, setReportOpen] = useState(false);
  const [versionHistory, setVersionHistory] = useState<VersionHistoryEntry[]>([]);
  const [versionHistoryOpen, setVersionHistoryOpen] = useState(false);
  const [redactedCount, setRedactedCount] = useState(0);
  const [entityMap, setEntityMap] = useState<Record<string, string>>({});
  const [redactionVersion, setRedactionVersion] = useState(0);
  const [resetConfirmOpen, setResetConfirmOpen] = useState(false);
  const latestFileIdRef = useRef<string | null>(null);
  const asyncResultEpochRef = useRef(0);
  const redactionInFlightRef = useRef(false);

  // Issue#194：已提交未收尾的打码任务（done/failed/换文件/重置时收尾）。
  // 回执无 file_id（controller 交接约束）：fileId 一直以 fileCtx 现有状态为准，
  // 这里记 fileId 只为「换文件后不跟进旧任务的结果」。
  // 终审 C：用 state 而非 ref——草稿落盘 effect 的依赖里要有它，提交打码后落盘的
  // 草稿才能带上 pendingRedactJobId（刷新恢复的唯一凭据）。
  type PendingRedact = {
    fileId: string;
    jobId: string;
    /** 提交时前端点名的处理项数：快照不可用时兜底 redactedCount 展示。 */
    requestedCount: number;
  };
  const [pendingRedact, setPendingRedact] = useState<PendingRedact | null>(null);
  /** 用户主动放弃会话（取消/重置）时登记的 job：provider 报 failed 时静默收尾。 */
  const redactAbandonedJobsRef = useRef<Set<string>>(new Set());

  // 化名映射确认（替换模式）：原文 → 化名。用户编辑过的行不被自动补全覆盖。
  const [pseudonymMap, setPseudonymMap] = useState<Record<string, string>>({});
  const [pseudonymMapLoading, setPseudonymMapLoading] = useState(false);
  const [pseudonymMapError, setPseudonymMapError] = useState<string | null>(null);
  const [pseudonymRetryTick, setPseudonymRetryTick] = useState(0);
  // 替换模式执行过（结果页据此展示「下载化名对照表」）；对照表内容用
  // 执行响应的 entity_map（后端真实替换结果，含 coref 复用），与成品天然一致
  const [confirmedPseudonymMap, setConfirmedPseudonymMap] = useState<Record<string, string> | null>(
    null,
  );
  const pseudonymEpochRef = useRef(0);
  // 用户在映射表手动改过的原文键：重新识别刷新自动映射时不覆盖
  const pseudonymUserEditedRef = useRef<Set<string>>(new Set());
  // 上次成功拉取映射时的实体集签名：变化（重新识别）则刷新全部自动映射
  const pseudonymEntitySigRef = useRef<string>('');

  const getRecognitionBlocker = useCallback(
    (file: { fileType: string; isScanned: boolean; content: string }) => {
      if (!health || healthChecking) return null;

      const requiredServices = new Set<ServiceKey>();
      const isImage = file.fileType === 'image' || file.isScanned;
      if (isImage) {
        if (latestOcrHasTypesRef.current.length > 0) {
          requiredServices.add('paddle_ocr');
          requiredServices.add('has_ner');
        }
        if (latestVisualFeatureTypesRef.current.length > 0) {
          requiredServices.add('visual_features');
        }
      } else if (file.content && latestSelectedTypesRef.current.length > 0) {
        requiredServices.add('has_ner');
      }

      const blocked = [...requiredServices].filter((key) => isServiceBlocked(health, key));
      if (blocked.length === 0) return null;

      return t('playground.recognitionPausedModelServices').replace(
        '{services}',
        blocked.map((key) => serviceLabel(health, key)).join(', '),
      );
    },
    [health, healthChecking, latestSelectedTypesRef],
  );

  // Issue#194 验收反馈：completed 单文件回场恢复结果页——实现在 fileCtx 之后
  // 赋值（latest-ref 模式），options 字面量先引用 trampoline。
  const resumeCompletedResultRef = useRef<
    (fileId: string, info: Record<string, unknown>) => Promise<boolean>
  >(async () => false);

  const fileCtx = usePlaygroundFile({
    latestOcrHasTypesRef,
    latestVisualFeatureTypesRef,
    latestSelectedTypesRef,
    resetEntityHistory: entityCtx.entityHistory.reset,
    resetImageHistory: () => imageCtx.imageHistory.reset(),
    setEntities: entityCtx.setEntities,
    setSelectedTypes: recognition.setSelectedTypes,
    setBoundingBoxes: (val) => imageCtx.setBoundingBoxes(val),
    getRecognitionBlocker,
    resumeCompletedResult: (fileId, info) => resumeCompletedResultRef.current(fileId, info),
  });

  // Issue #83：预览范式由文件形态决定——文本型 PDF（不分打码/替换）都走页
  // 面视图，派生一次多处复用（评审 M7：工作台/历史语义/执行阈值必须同源）。
  // textPdfMaskMode 保留执行语义分叉：框选中同步（syncEntitiesWithNerBoxes）
  // 与撤销/重做只属于打码工作台，替换模式画布只读、执行仍走实体勾选。
  const textPdfVisualMode = useMemo(
    () =>
      !fileCtx.isImageMode &&
      isVisualPreviewMode(fileCtx.fileInfo?.file_type, Boolean(fileCtx.fileInfo?.is_scanned)),
    [fileCtx.isImageMode, fileCtx.fileInfo?.file_type, fileCtx.fileInfo?.is_scanned],
  );
  const textPdfMaskMode = textPdfVisualMode && recognition.processingMode === 'mask';

  const imageCtx = usePlaygroundImage({
    fileInfo: fileCtx.fileInfo,
    redactionVersion,
    showRedactedPreview: fileCtx.stage === 'result',
    // Issue #83：文本型 PDF 两种模式都加载页面图（页面视图底座）
    staticPagePreview: textPdfVisualMode,
  });

  // Issue #66 验收反馈、#83 扩展：识别实体自动成框（手拉框只是兜底）。
  // 文本 PDF 页面视图下（两种处理方式），识别完成/实体集变化时调用后端
  // locate-entities（与执行链路共用定位核心，所见即所打），ner 框并入
  // boundingBoxes 展示；手拉框（manual）保留。按实体文本签名去重，避免
  // 模式来回切换反复请求。
  const locatedSignatureRef = useRef<string | null>(null);
  const locateEpochRef = useRef(0);
  useEffect(() => {
    const fileId = fileCtx.fileInfo?.file_id;
    const entities = entityCtx.entities;
    const signature =
      fileId && textPdfVisualMode && entities.length
        ? fileId + ':' + [...new Set(entities.map((e) => e.text))].join('\u0001')
        : null;
    if (signature === locatedSignatureRef.current) return;
    if (!signature) return;
    const epoch = ++locateEpochRef.current;
    const entityByText = new Map(entities.map((e) => [e.text, e]));
    locateEntityBoxes(fileId!, entities)
      .then(({ boxes, missed }) => {
        if (epoch !== locateEpochRef.current) return;
        // 签名只在成功后写入——失败的定位（超时等）在实体/模式下次变化时
        // 会自动重试，不会因签名已占位而永远沉默
        locatedSignatureRef.current = signature;
        imageCtx.setBoundingBoxes((prev) =>
          mergeNerBoxes(prev, boxes, entityByText),
        );
        if (missed.length) {
          showToast(
            t('playground.locateMissed').replace('{n}', String(missed.length)),
            'info',
          );
        }
      })
      .catch(() => {
        if (epoch === locateEpochRef.current) {
          showToast(t('playground.locateFailed'), 'error');
        }
      });
    // setBoundingBoxes 稳定；entities 以签名为准避免每词抖动重触发
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [textPdfVisualMode, fileCtx.fileInfo?.file_id, entityCtx.entities]);

  const { setTypeTab } = recognition;
  useEffect(() => {
    setTypeTab(fileCtx.isImageMode ? 'vision' : 'text');
  }, [fileCtx.isImageMode, setTypeTab]);

  // 文件切换：在途异步结果一律失效（epoch 前进）。只随 file_id 变化——终审 C 后
  // pendingRedact 不再并入本 effect（提交打码/收尾清守门都会变它，若并入会把
  // done 分支刚登记的 epoch 顶掉，结果页快照回填全被丢弃）。
  useEffect(() => {
    latestFileIdRef.current = fileCtx.fileInfo?.file_id ?? null;
    asyncResultEpochRef.current += 1;
  }, [fileCtx.fileInfo?.file_id]);

  // 换文件/重置：上一文件的在途打码不再跟进（任务后台继续，完成由 provider 通知）
  useEffect(() => {
    if (pendingRedact && pendingRedact.fileId !== latestFileIdRef.current) {
      redactionInFlightRef.current = false;
      setPendingRedact(null);
    }
  }, [pendingRedact]);

  const allSelectedVisionTypes = useMemo(
    () => [...recognition.selectedOcrHasTypes, ...recognition.selectedVisualFeatureTypes],
    [recognition.selectedOcrHasTypes, recognition.selectedVisualFeatureTypes],
  );

  const historyCtx = usePlaygroundHistory({
    // Issue #66：视觉工作台（含文本型 PDF 打码模式）下撤销/重做/全选作用于
    // 拉框；实体勾选仍走右侧面板逐条切换
    isImageMode: fileCtx.isImageMode || textPdfMaskMode,
    entities: entityCtx.entities,
    setEntities: entityCtx.setEntities,
    boundingBoxes: imageCtx.boundingBoxes,
    visibleBoxes: imageCtx.visibleBoxes,
    setBoundingBoxes: imageCtx.setBoundingBoxes,
    entityHistory: entityCtx.entityHistory,
    imageHistory: imageCtx.imageHistory,
    allSelectedVisionTypes,
  });

  const canApplyAsyncResult = useCallback((fileId: string, epoch: number) => {
    return latestFileIdRef.current === fileId && asyncResultEpochRef.current === epoch;
  }, []);

  // Destructured so handleRerunNer can depend on the exact fields it uses
  // instead of the whole (per-render) ctx objects.
  const { setRecognitionIssue } = fileCtx;
  const { handleRerunNerImage } = imageCtx;
  const { handleRerunNerText } = entityCtx;

  const handleRerunNer = useCallback(async () => {
    if (!fileCtx.fileInfo) return;
    const blocker = getRecognitionBlocker({
      fileType: fileCtx.fileInfo.file_type || '',
      isScanned: Boolean(fileCtx.fileInfo.is_scanned),
      content: fileCtx.content,
    });
    if (blocker) {
      setRecognitionIssue(blocker);
      showToast(blocker, 'info');
      return;
    }
    setRecognitionIssue(null);
    if (fileCtx.isImageMode) {
      await handleRerunNerImage(
        fileCtx.fileInfo.file_id,
        recognition.selectedOcrHasTypes,
        recognition.selectedVisualFeatureTypes,
        fileCtx.setIsLoading,
        fileCtx.setLoadingMessage,
        fileCtx.setVisionStream,
      );
    } else {
      await handleRerunNerText(
        fileCtx.fileInfo.file_id,
        recognition.selectedTypesRef.current,
        fileCtx.setIsLoading,
        fileCtx.setLoadingMessage,
      );
    }
  }, [
    fileCtx.content,
    fileCtx.fileInfo,
    fileCtx.isImageMode,
    fileCtx.setIsLoading,
    fileCtx.setLoadingMessage,
    fileCtx.setVisionStream,
    getRecognitionBlocker,
    handleRerunNerImage,
    handleRerunNerText,
    recognition.selectedOcrHasTypes,
    recognition.selectedTypesRef,
    recognition.selectedVisualFeatureTypes,
    setRecognitionIssue,
  ]);

  // 替换模式下自动补默认化名：仅对缺失的原文 key 请求 preview-map，
  // 合并时不覆盖已有（可能已被用户编辑）的行。
  const selectedEntityTexts = useMemo(
    () =>
      Array.from(
        new Set(entityCtx.entities.filter((e) => e.selected !== false).map((e) => e.text)),
      ).filter(Boolean),
    [entityCtx.entities],
  );
  const missingPseudonymKeys = useMemo(
    () => selectedEntityTexts.filter((text) => !(text in pseudonymMap)),
    [selectedEntityTexts, pseudonymMap],
  );
  const entitySignature = useMemo(
    () => selectedEntityTexts.join('\u0000'),
    [selectedEntityTexts],
  );
  useEffect(() => {
    if (recognition.processingMode !== 'replace') return;
    if (fileCtx.isImageMode) return;
    const sigChanged = pseudonymEntitySigRef.current !== entitySignature;
    // 全部行已补齐且实体集未变化（可能含失败后手动填全的情况）时清掉残留错误
    if (missingPseudonymKeys.length === 0 && !sigChanged) {
      setPseudonymMapError(null);
      return;
    }
    const epoch = ++pseudonymEpochRef.current;
    const controller = new AbortController();
    setPseudonymMapLoading(true);
    setPseudonymMapError(null);
    const run = async () => {
      try {
        const res = await authFetch('/api/v1/redaction/preview-map', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            // 完整透传实体（含 coref_id）：化名模式后端已改为严格按原文分配，
            // coref_id 仅作透传保留（结构化等模式仍按 coref 复用），保持请求契约不变
            entities: entityCtx.entities
              .filter((e) => e.selected !== false)
              .map((e) => ({ ...e, selected: true })),
            config: { replacement_mode: 'pseudonym' },
          }),
          signal: controller.signal,
        });
        if (!res.ok) throw new Error(t('playground.pseudonymLoadFailed'));
        const data = await safeJson<{ entity_map?: Record<string, string> }>(res);
        if (epoch !== pseudonymEpochRef.current) return;
        const incoming = data.entity_map ?? {};
        pseudonymEntitySigRef.current = entitySignature;
        setPseudonymMap((current) => {
          const next = { ...current };
          for (const [key, value] of Object.entries(incoming)) {
            // 只补缺；实体集变化（重新识别）时刷新全部「非用户手改」键，
            // 使服务端化名规则升级后旧自动映射能被纠正
            if (!(key in next) || (sigChanged && !pseudonymUserEditedRef.current.has(key))) {
              next[key] = value;
            }
          }
          return next;
        });
      } catch (err) {
        if (controller.signal.aborted) return;
        if (epoch !== pseudonymEpochRef.current) return;
        setPseudonymMapError(localizeErrorMessage(err, 'playground.pseudonymLoadFailed'));
      } finally {
        if (epoch === pseudonymEpochRef.current) setPseudonymMapLoading(false);
      }
    };
    void run();
    return () => controller.abort();
  }, [
    entitySignature,
    missingPseudonymKeys.length,
    recognition.processingMode,
    fileCtx.isImageMode,
    entityCtx.entities,
    pseudonymRetryTick,
  ]);

  const retryPseudonymLoad = useCallback(() => {
    setPseudonymMapError(null);
    setPseudonymRetryTick((tick) => tick + 1);
  }, []);

  const setPseudonymReplacement = useCallback((text: string, replacement: string) => {
    pseudonymUserEditedRef.current.add(text);
    setPseudonymMap((current) => ({ ...current, [text]: replacement }));
  }, []);

  // 已选实体的范围内，不同原文映射到同一非空化名 → 冲突（警告展示用）
  const pseudonymConflicts = useMemo(() => {
    const inScope = new Set(selectedEntityTexts);
    const byReplacement = new Map<string, string[]>();
    for (const [text, replacement] of Object.entries(pseudonymMap)) {
      if (!inScope.has(text)) continue;
      const key = replacement.trim();
      if (!key) continue;
      const list = byReplacement.get(key) ?? [];
      list.push(text);
      byReplacement.set(key, list);
    }
    const conflicted = new Set<string>();
    for (const texts of byReplacement.values()) {
      if (texts.length > 1) texts.forEach((text) => conflicted.add(text));
    }
    return conflicted;
  }, [pseudonymMap, selectedEntityTexts]);

  // 注：旧版这里有「共指组内替换词不一致禁执行」门槛。后端化名分配已改为严格
  // 按原文（不同原文即使被模型误标同组也不再共享化名，见 replacement_strategy
  // 的 PSEUDONYM 分支），不存在静默覆盖，别名统一由用户直接在映射表填同一个词，
  // 该门槛随之移除。

  // 替换模式执行门槛：默认化名仍在生成、生成失败、或有已选实体的映射被清空时，
  // 不允许执行——避免成品与用户在 UI 确认的映射不一致
  const replaceUnready = useMemo(
    () =>
      recognition.processingMode === 'replace' &&
      !fileCtx.isImageMode &&
      (pseudonymMapLoading ||
        Boolean(pseudonymMapError) ||
        selectedEntityTexts.some((text) => !(pseudonymMap[text] ?? '').trim())),
    [
      recognition.processingMode,
      fileCtx.isImageMode,
      pseudonymMapLoading,
      pseudonymMapError,
      selectedEntityTexts,
      pseudonymMap,
    ],
  );

  const presetSeqRef = useRef(recognition.presetApplySeq);
  useEffect(() => {
    if (recognition.presetApplySeq === presetSeqRef.current) return;
    presetSeqRef.current = recognition.presetApplySeq;
    if (!fileCtx.fileInfo || fileCtx.isLoading) return;
    if (fileCtx.stage !== 'preview') return;
    void handleRerunNer();
  }, [
    recognition.presetApplySeq,
    fileCtx.fileInfo,
    fileCtx.isLoading,
    fileCtx.stage,
    handleRerunNer,
  ]);

  // Issue#194 验收反馈（sulan 场景）：打码提交失败时先查一次文件——成品在说明
  // 其实已经打完（常见于完成后回到确认页又点了一次），直接恢复结果页并返回 true。
  const recoverCompletedRedact = useCallback(async (fileId: string): Promise<boolean> => {
    try {
      const response = await authFetch(`/api/v1/files/${fileId}`);
      if (!response.ok) return false;
      const info = await safeJson<Record<string, unknown>>(response);
      if (!info.output_path) return false;
      return await resumeCompletedResultRef.current(fileId, info);
    } catch {
      return false;
    }
  }, []);

  const handleRedact = useCallback(async () => {
    if (!fileCtx.fileInfo) return;
    if (redactionInFlightRef.current) return;
    // 替换模式映射未确认完（生成中/失败/有空值）不允许执行，保证成品即所见
    if (replaceUnready) {
      showToast(t('playground.pseudonymConfirmRequired'), 'info');
      return;
    }

    // Issue#194（D3「点完打码即可离开」）：提交后台任务后即返回——不切 stage、
    // 不设 loading/遮罩；完成由 provider 轮询推进（下方订阅 effect 收尾进 result）。
    // 任务提交本身不可中止：redactionInFlightRef 只防双击/重复提交。
    redactionInFlightRef.current = true;
    const fileId = fileCtx.fileInfo.file_id;

    try {
      // 任务句柄：识别段已注册（T9）。provider 挂载恢复异步回填 fileId，一拍 miss
      // 容忍：先问一次，miss 则 refresh 后再问（loadExistingFile 同款）。
      let task = getSingleTaskRef.current(fileId);
      if (!task || !task.itemId) {
        await refreshSingleTasksRef.current();
        task = getSingleTaskRef.current(fileId);
      }
      let jobId = task?.jobId ?? '';
      let itemId = task?.itemId ?? '';
      if (!jobId || !itemId) {
        // Bug#2 刷新后兜底：provider 账本只恢复 active 任务，识别完成停稳在
        // awaiting_review 的任务不在其中（watch 未注册）⇒ 按 fileId 直查后端
        // awaiting_review 清单。命中后 watch 注册回账本——完成推进与正常路径
        // 同轨（provider 轮询可接手收尾进结果页）。
        const fallback = await resolveAwaitingReviewTask(fileId);
        if (fallback) {
          watchSingleTaskRef.current(
            fallback.job_id,
            fileId,
            fileCtx.fileInfo?.filename || fileId,
          );
          jobId = fallback.job_id;
          itemId = fallback.item_id;
        }
      }
      if (!jobId || !itemId) {
        throw new Error(t('playground.redactFailed'));
      }

      // Issue#199（泄漏安全）：识别存在失败页时，打码前必须知情确认——失败页
      // 零区域 = 零脱敏，不得被默读为「该页无敏感内容」。查询失败不阻塞主流程
      // （守卫是加严面）；用户取消则中止本次提交。
      try {
        const detail = await getJob(jobId, { performance: true });
        const item =
          detail.items.find((candidate) => candidate.file_id === fileId) ?? detail.items[0];
        const failedPages = recognitionFailedPages(item?.performance ?? null);
        if (failedPages.length > 0) {
          const confirmed = window.confirm(
            t('playground.redactFailedPagesConfirm').replace('{pages}', failedPages.join(', ')),
          );
          if (!confirmed) {
            redactionInFlightRef.current = false;
            return;
          }
        }
      } catch {
        // 性能查询失败 ⇒ 无从判定失败页 ⇒ 放行主流程（后端守卫仍在：skip_review 抑制）
      }

      // Issue #66：mask 模式下实体的选中状态由它的 ner 框代表（框即实体的
      // UI）；无框实体（定位失败）保持原选中态交后端 residual 兜底
      const execEntities = textPdfMaskMode
        ? syncEntitiesWithNerBoxes(entityCtx.entities, imageCtx.boundingBoxes)
        : entityCtx.entities;
      const selectedEntities = execEntities.filter((e) => e.selected !== false);
      const selectedBoxes = imageCtx.boundingBoxes.filter((b) => b.selected !== false);
      // 文本型 PDF 打码=实体+拉框双通道；ner 框与其实体同文本只计一次
      const nerBoxTexts = new Set(
        imageCtx.boundingBoxes.filter((b) => b.source === 'ner').map((b) => b.text ?? ''),
      );
      const requestedRedactionItemCount = fileCtx.isImageMode
        ? selectedBoxes.length
        : textPdfMaskMode
          ? selectedBoxes.length +
            selectedEntities.filter((e) => !nerBoxTexts.has(e.text)).length
          : selectedEntities.length;

      // Issue #79 v3：打码无子选项，固定 mask（文本型 PDF=栅格化涂黑框）；
      // 替换分支唯一方案=化名词池+对照表确认（pseudonym）。
      const isPseudonym = recognition.processingMode === 'replace' && !fileCtx.isImageMode;
      const effectiveReplacementMode = isPseudonym ? 'pseudonym' : 'mask';
      const pseudonymReplacements: Record<string, string> = {};
      if (isPseudonym) {
        for (const entity of selectedEntities) {
          const replacement = (pseudonymMap[entity.text] ?? '').trim();
          if (replacement) pseudonymReplacements[entity.text] = replacement;
        }
      }

      setPendingRedact({
        fileId,
        jobId,
        requestedCount: requestedRedactionItemCount,
      });
      // Issue #66 A 案：文本型文件在替换模式下不携带拉框（后端见到「文本 PDF + 有框」
      // 会整份转图像管线栅格化）；框保留在前端状态里，切回打码原样恢复参与执行。
      await submitRedactionTask(jobId, itemId, {
        entities: execEntities,
        bounding_boxes: boxesForRedactPayload(
          fileCtx.isImageMode,
          recognition.processingMode,
          imageCtx.boundingBoxes,
        ),
        config: {
          replacement_mode: effectiveReplacementMode,
          custom_replacements: pseudonymReplacements,
          watermark_text: recognition.watermarkText.trim() || undefined,
        },
      });
      if (latestFileIdRef.current !== fileId) {
        // 提交期间用户已切走/重置：任务后台继续，本会话不再跟进结果
        redactionInFlightRef.current = false;
        setPendingRedact(null);
        return;
      }
      // 替换模式执行过（结果页据此展示「下载化名对照表」）：提交成功即确认
      setConfirmedPseudonymMap(isPseudonym ? { ...pseudonymReplacements } : null);
      // passive 提示（不遮挡）：打码在后台进行，用户可离开本页
      showToast(t('playground.taskRedacting'), 'info');
    } catch (err) {
      redactionInFlightRef.current = false;
      setPendingRedact(null);
      // Issue#194 验收反馈（sulan 场景）：打码可能已在别处/上一会话完成（完成后
      // 回到确认页又点了一次打码，后端 400 not awaiting_review）。此时不弹失败——
      // 查一次文件：成品在就直接恢复结果页并提示，只有查无成品才走失败文案。
      if (await recoverCompletedRedact(fileId)) {
        showToast(t('playground.redactAlreadyDone'), 'info');
        return;
      }
      // 409（同账号其他任务在跑）等提交失败：toast 后端文案，停留 preview 不切 stage
      showToast(localizeErrorMessage(err, 'playground.redactFailed'), 'error');
    }
  }, [
    entityCtx.entities,
    fileCtx,
    imageCtx.boundingBoxes,
    recognition.processingMode,
    recognition.watermarkText,
    pseudonymMap,
    replaceUnready,
    textPdfMaskMode,
    recoverCompletedRedact,
  ]);

  // --- Issue#194：打码任务收尾订阅——provider 报 done/failed 时推进/退出 ---
  // 识别段任务在 awaiting_review 停稳后 taskRecognition 已清空，这里按「本会话
  // 提交过的打码 job」守门：只有当前文件 + 匹配 job 的迁移才驱动结果页。
  const currentFileId = fileCtx.fileInfo?.file_id ?? null;
  const { setStage: setFileStage } = fileCtx;
  useEffect(() => {
    if (!currentFileId) return;
    const pending = pendingRedact;
    if (!pending || pending.fileId !== currentFileId) return;
    const task = getSingleTask(currentFileId);
    if (!task || task.jobId !== pending.jobId) return;

    if (task.phase === 'failed') {
      setPendingRedact(null);
      redactionInFlightRef.current = false;
      if (redactAbandonedJobsRef.current.has(task.jobId)) return; // 用户已放弃会话，静默
      showToast(task.error || t('playground.redactFailed'), 'error');
      return;
    }
    if (task.phase !== 'done') return;

    // 打码完成（provider 已发完成通知）：快照 + report/versions → 结果页
    setPendingRedact(null);
    redactionInFlightRef.current = false;
    latestFileIdRef.current = currentFileId;
    const asyncResultEpoch = asyncResultEpochRef.current + 1;
    asyncResultEpochRef.current = asyncResultEpoch;
    setRedactionVersion((version) => version + 1); // 触发 result 阶段脱敏预览重取（同旧流）
    setFileStage('result');

    const loadAsyncResult = async <T,>(url: string): Promise<T> => {
      const response = await authFetch(url);
      if (!response.ok) {
        throw new Error(`Failed to load ${url}`);
      }
      return safeJson<T>(response);
    };

    // entity_map / redacted_count 数据源 = GET /files/{id} 快照：打码工人落库的
    // 真实结果（execute_redaction 写 file_store 的 entity_map/redacted_count），
    // 等价于旧同步 execute 响应的对应字段；快照不可用则回落提交时的点名数。
    loadAsyncResult<Record<string, unknown>>(`/api/v1/files/${currentFileId}`)
      .then((snapshot) => {
        if (!canApplyAsyncResult(currentFileId, asyncResultEpoch)) return;
        const map = (snapshot.entity_map ?? {}) as Record<string, string>;
        setEntityMap(map);
        const count = snapshot.redacted_count;
        setRedactedCount(
          typeof count === 'number' ? count : pending.requestedCount,
        );
      })
      .catch(() => {
        if (!canApplyAsyncResult(currentFileId, asyncResultEpoch)) return;
        setEntityMap({});
        setRedactedCount(pending.requestedCount);
      });

    loadAsyncResult<Record<string, unknown>>(`/api/v1/redaction/${currentFileId}/report`)
      .then((data) => {
        if (canApplyAsyncResult(currentFileId, asyncResultEpoch)) {
          setRedactionReport(data);
        }
      })
      .catch(() => {
        if (canApplyAsyncResult(currentFileId, asyncResultEpoch)) {
          setRedactionReport(null);
        }
      });

    loadAsyncResult<{ versions?: VersionHistoryEntry[] }>(
      `/api/v1/redaction/${currentFileId}/versions`,
    )
      .then((data) => {
        if (canApplyAsyncResult(currentFileId, asyncResultEpoch)) {
          setVersionHistory(data.versions || []);
        }
      })
      .catch(() => {
        if (canApplyAsyncResult(currentFileId, asyncResultEpoch)) {
          setVersionHistory([]);
        }
      });
  }, [
    canApplyAsyncResult,
    currentFileId,
    getSingleTask,
    pendingRedact,
    setFileStage,
  ]);

  // Issue#194 验收反馈：打码已完成的单文件从任务中心/历史回场——镜像 done 订阅的
  // 结果组装（快照 entity_map/redacted_count + report + versions），直接恢复结果页。
  // 由 loadExistingFile 在 file 记录带 output_path 时调用（info 复用其已取快照）。
  useEffect(() => {
    resumeCompletedResultRef.current = async (fileId: string, info: Record<string, unknown>) => {
      if (!info.output_path) return false;
      latestFileIdRef.current = fileId;
      asyncResultEpochRef.current += 1;
      const epoch = asyncResultEpochRef.current;
      setRedactionVersion((version) => version + 1); // 触发 result 阶段脱敏预览重取
      fileCtx.setStage('result');
      const map = (info.entity_map ?? {}) as Record<string, string>;
      setEntityMap(map);
      const count = info.redacted_count;
      setRedactedCount(typeof count === 'number' ? count : 0);
      const load = async <T,>(url: string): Promise<T | null> => {
        try {
          const response = await authFetch(url);
          if (!response.ok) return null;
          return await safeJson<T>(response);
        } catch {
          return null;
        }
      };
      void load<Record<string, unknown>>(`/api/v1/redaction/${fileId}/report`).then((data) => {
        if (canApplyAsyncResult(fileId, epoch)) setRedactionReport(data);
      });
      void load<{ versions?: VersionHistoryEntry[] }>(
        `/api/v1/redaction/${fileId}/versions`,
      ).then((data) => {
        if (canApplyAsyncResult(fileId, epoch)) setVersionHistory(data?.versions ?? []);
      });
      return true;
    };
  });

  const cancelProcessing = useCallback(() => {
    asyncResultEpochRef.current += 1;
    // 在途打码同会话放弃：登记 job（provider 报 failed 时静默收尾）
    if (pendingRedact) {
      redactAbandonedJobsRef.current.add(pendingRedact.jobId);
      setPendingRedact(null);
    }
    redactionInFlightRef.current = false;
    fileCtx.cancelProcessing(false);
    entityCtx.cancelRerunNerText();
    imageCtx.cancelRerunNerImage();
    fileCtx.setIsLoading(false);
    fileCtx.setLoadingMessage('');
    showToast(t('playground.cancelled'), 'info');
  }, [entityCtx, fileCtx, imageCtx, pendingRedact]);

  const hasResetRisk = useMemo(
    () =>
      fileCtx.stage !== 'upload' ||
      fileCtx.fileInfo !== null ||
      fileCtx.content.length > 0 ||
      entityCtx.entities.length > 0 ||
      imageCtx.boundingBoxes.length > 0 ||
      redactedCount > 0 ||
      Object.keys(entityMap).length > 0 ||
      redactionReport !== null ||
      versionHistory.length > 0,
    [
      entityCtx.entities.length,
      entityMap,
      fileCtx.content.length,
      fileCtx.fileInfo,
      fileCtx.stage,
      imageCtx.boundingBoxes.length,
      redactedCount,
      redactionReport,
      versionHistory.length,
    ],
  );

  const performReset = useCallback(() => {
    asyncResultEpochRef.current += 1;
    pseudonymEpochRef.current += 1;
    latestFileIdRef.current = null;
    // 在途打码同会话放弃：登记 job（provider 报 failed 时静默收尾）
    if (pendingRedact) {
      redactAbandonedJobsRef.current.add(pendingRedact.jobId);
      setPendingRedact(null);
    }
    redactionInFlightRef.current = false;
    // 与 applyDraftSnapshot 同享防护：取消在途识别，防止 R2（重置后重跑）
    // 与手动重置路径被旧文件识别回调污染。
    fileCtx.cancelProcessing(false);
    entityCtx.cancelRerunNerText();
    imageCtx.cancelRerunNerImage();
    setResetConfirmOpen(false);
    fileCtx.setStage('upload');
    fileCtx.setFileInfo(null);
    fileCtx.setContent('');
    entityCtx.setEntities([]);
    setRedactedCount(0);
    setEntityMap({});
    setPseudonymMap({});
    pseudonymUserEditedRef.current = new Set();
    pseudonymEntitySigRef.current = '';
    setPseudonymMapLoading(false);
    setPseudonymMapError(null);
    setConfirmedPseudonymMap(null);
    // 新文件回到默认处理方式（打码），与"识别后默认匿名化"的既有行为一致
    setRecognitionProcessingMode('mask');
    setRedactionVersion(0);
    setRedactionReport(null);
    setReportOpen(false);
    entityCtx.entityHistory.reset();
    imageCtx.setBoundingBoxes([]);
    imageCtx.imageHistory.reset();
    setVersionHistory([]);
    setVersionHistoryOpen(false);
    removeStorageItem(scopedStorageKey(STORAGE_KEYS.PLAYGROUND_DRAFT, ownerKey));
  }, [entityCtx, fileCtx, imageCtx, ownerKey, pendingRedact, setRecognitionProcessingMode]);

  // 会话草稿（Issue #33）：有活动文件时防抖落盘；显式重置时清除。
  // 上传新文件后本 effect 随 fileInfo 变化自然覆盖旧草稿。
  useEffect(() => {
    if (!fileCtx.fileInfo) return;
    const timer = setTimeout(() => {
      const snapshot = buildDraftSnapshot({
        stage: fileCtx.stage,
        fileInfo: fileCtx.fileInfo!,
        content: fileCtx.content,
        entities: entityCtx.entities,
        boundingBoxes: imageCtx.boundingBoxes,
        processingMode: recognition.processingMode,
        replacementMode: recognition.replacementMode,
        watermarkText: recognition.watermarkText,
        pseudonymMap,
        pseudonymUserEditedKeys: [...pseudonymUserEditedRef.current],
        confirmedPseudonymMap,
        entityMap,
        redactedCount,
        currentPage: imageCtx.currentPage,
        visionStream: fileCtx.visionStream,
        taskJobId: fileCtx.taskRecognition?.jobId,
        // Issue#194 终审 C：打码在途时落盘凭据——刷新后据此重挂收尾订阅（结果页可达）
        pendingRedactJobId:
          pendingRedact && pendingRedact.fileId === fileCtx.fileInfo!.file_id
            ? pendingRedact.jobId
            : undefined,
      });
      const json = serializeDraft(snapshot);
      if (json === null) return; // 超限：放弃持久化，内存会话不受影响
      setScopedStorageItem(STORAGE_KEYS.PLAYGROUND_DRAFT, json, ownerKey);
    }, 400);
    return () => clearTimeout(timer);
  }, [
    fileCtx.fileInfo,
    fileCtx.stage,
    fileCtx.visionStream,
    fileCtx.content,
    fileCtx.taskRecognition,
    entityCtx.entities,
    imageCtx.boundingBoxes,
    imageCtx.currentPage,
    recognition.processingMode,
    recognition.replacementMode,
    recognition.watermarkText,
    pseudonymMap,
    confirmedPseudonymMap,
    entityMap,
    redactedCount,
    pendingRedact,
    ownerKey,
  ]);

  // 把草稿快照整体恢复为当前会话（挂载恢复与历史页「回到现场」共用）。
  // 恢复是幂等的：undo 栈重置、dialog 态一律回到关闭，epoch 前进使在途异步结果失效。
  const applyDraftSnapshot = useCallback(
    (snapshot: PlaygroundDraftSnapshot) => {
      asyncResultEpochRef.current += 1;
      latestFileIdRef.current = snapshot.fileInfo.file_id;
      redactionInFlightRef.current = false;
      // 取消在途识别（Provider 全局化后可跨页在途）：否则旧文件的
      // pendingFile 识别/重跑/图片检测完成后会无条件 setEntities+setStage，
      // 把旧文件实体灌进新恢复的会话（跨文件串染）。
      fileCtx.cancelProcessing(false);
      entityCtx.cancelRerunNerText();
      imageCtx.cancelRerunNerImage();
      fileCtx.setFileInfo(snapshot.fileInfo);
      fileCtx.setContent(snapshot.content);
      fileCtx.setStage(snapshot.stage);
      entityCtx.setEntities(snapshot.entities);
      entityCtx.entityHistory.reset();
      imageCtx.setBoundingBoxes(snapshot.boundingBoxes);
      imageCtx.imageHistory.reset();
      imageCtx.setCurrentPage(snapshot.currentPage);
      setEntityMap(snapshot.entityMap);
      setRedactedCount(snapshot.redactedCount);
      setRedactionVersion((version) => version + 1); // 触发 result 阶段脱敏预览图重取
      setPseudonymMap(snapshot.pseudonymMap);
      pseudonymUserEditedRef.current = new Set(snapshot.pseudonymUserEditedKeys ?? []);
      pseudonymEntitySigRef.current = '';
      setPseudonymMapLoading(false);
      setPseudonymMapError(null);
      setConfirmedPseudonymMap(snapshot.confirmedPseudonymMap);
      // 顺序约束：setReplacementMode 对非 'pseudonym' 值会连带置 processingMode='mask'，
      // 故必须先调它、最后调 setProcessingMode，否则替换模式会话会被恢复成打码模式。
      recognition.setReplacementMode(snapshot.replacementMode);
      recognition.setProcessingMode(snapshot.processingMode);
      recognition.setWatermarkText(snapshot.watermarkText);
      // Issue#194：草稿里带在途识别任务 → 重挂 provider（watch 幂等；任务若已终态，
      // 订阅 effect 的 redacting/done/failed 分支会兜底退出识别中视图）。
      // 注意放在最后：上面的 cancelProcessing(false) 会清掉旧任务态。
      if (snapshot.taskJobId && snapshot.fileInfo?.file_id) {
        fileCtx.reattachTask(
          snapshot.taskJobId,
          snapshot.fileInfo.file_id,
          snapshot.fileInfo.filename,
        );
      }
      // Issue#194 终审 C（AC1）：打码在途的草稿——收尾守门是纯内存，刷新后必须重挂：
      // pendingRedact 登记回订阅守门 + watch 重挂 provider 账本。任务在刷新期间已
      // completed ⇒ provider 轮询报 done ⇒ 订阅 effect 照常拉快照+report 收尾进结果页；
      // failed ⇒ 报错退出；仍在打码 ⇒ 继续等（点完打码即可离开的语义不受影响）。
      if (
        snapshot.pendingRedactJobId &&
        snapshot.fileInfo?.file_id &&
        snapshot.stage !== 'result'
      ) {
        setPendingRedact({
          fileId: snapshot.fileInfo.file_id,
          jobId: snapshot.pendingRedactJobId,
          requestedCount: snapshot.redactedCount,
        });
        watchSingleTaskRef.current(
          snapshot.pendingRedactJobId,
          snapshot.fileInfo.file_id,
          snapshot.fileInfo.filename,
        );
      }
      setResetConfirmOpen(false);
      setReportOpen(false);
      setVersionHistoryOpen(false);
      // result 阶段恢复时报告/版本历史不入草稿，需要重取（失败静默回落）。
      // 守卫用 latestFileIdRef 而非 canApplyAsyncResult：file_id effect
      // （[fileCtx.fileInfo?.file_id]）在恢复时必然再 bump epoch，epoch 守卫
      // 恒 false 会击穿应用；改判「本会话仍是这个文件」——恢复时 ref 已指向
      // 目标文件，响应返回即应用；用户又切走则 ref 已变 → 丢弃陈旧响应。
      if (snapshot.stage === 'result') {
        const resultFileId = snapshot.fileInfo.file_id;
        const applyResult = async <T,>(
          url: string,
          apply: (data: T) => void,
          fallback: () => void,
        ) => {
          try {
            const res = await authFetch(url);
            if (!res.ok) throw new Error(String(res.status));
            const data = await safeJson<T>(res);
            if (latestFileIdRef.current === resultFileId) apply(data);
          } catch {
            if (latestFileIdRef.current === resultFileId) fallback();
          }
        };
        void applyResult<Record<string, unknown>>(
          `/api/v1/redaction/${resultFileId}/report`,
          setRedactionReport,
          () => setRedactionReport(null),
        );
        void applyResult<{ versions?: VersionHistoryEntry[] }>(
          `/api/v1/redaction/${resultFileId}/versions`,
          (data) => setVersionHistory(data.versions || []),
          () => setVersionHistory([]),
        );
      }
    },
    [entityCtx, fileCtx, imageCtx, recognition],
  );

  // 挂载恢复（R1）：每个应用生命周期只做一次；幂等，StrictMode 双挂载无害。
  // URL 带 ?file_id=（历史页「回到现场」的明确意图）时跳过：避免先展示旧草稿现场
  // 再弹「切换处理文件」确认框——用户点 A 却先看到 B，观感即「跳错文件」。
  const draftRestoreDoneRef = useRef(false);
  useEffect(() => {
    if (draftRestoreDoneRef.current) return;
    draftRestoreDoneRef.current = true;
    if (new URLSearchParams(window.location.search).get('file_id')) return;
    const snapshot = parseDraft(
      getScopedStorageItem<string | null>(STORAGE_KEYS.PLAYGROUND_DRAFT, null, ownerKey),
    );
    if (!snapshot) return;
    applyDraftSnapshot(snapshot);
    showToast(t('playground.restored'), 'info');
  }, [applyDraftSnapshot, ownerKey]);

  // 历史页「回到现场」入口（R2 + 草稿恢复）：?file_id= 协议统一走这里。
  // 有本文件的草稿 → 直接恢复现场；无草稿 → 重置后按当前识别配置重新识别。
  const resumeFromFile = useCallback(
    async (targetFileId: string) => {
      const snapshot = parseDraft(
        getScopedStorageItem<string | null>(STORAGE_KEYS.PLAYGROUND_DRAFT, null, ownerKey),
      );
      const decision = planResume({ targetFileId, snapshot });
      if (decision.mode === 'unavailable') return;
      if (decision.mode === 'draft') {
        applyDraftSnapshot(decision.snapshot);
        showToast(t('playground.restored'), 'info');
        return;
      }
      // R2：无草稿（或草稿属于其他文件）→ 重置后恢复：命中服务端识别缓存则秒回
      // （loadExistingFile 内 toast「已从服务端恢复」），未识别过才走识别 loading——
      // 不在此处预告文案，避免「未找到现场」+「已从服务端恢复」双 toast 矛盾
      performReset();
      await fileCtx.loadExistingFile(decision.fileId);
    },
    [applyDraftSnapshot, fileCtx, ownerKey, performReset],
  );

  const handleReset = useCallback(() => {
    if (hasResetRisk) {
      setResetConfirmOpen(true);
      return;
    }
    performReset();
  }, [hasResetRisk, performReset]);

  const confirmReset = useCallback(() => {
    performReset();
  }, [performReset]);

  const cancelReset = useCallback(() => {
    setResetConfirmOpen(false);
  }, []);

  const handleDownload = useCallback(() => {
    if (!fileCtx.fileInfo) return;
    // Returns the promise so callers can show a busy state while fetching.
    return downloadFile(
      `/api/v1/files/${fileCtx.fileInfo.file_id}/download?redacted=true`,
      `redacted_${fileCtx.fileInfo.filename}`,
    ).catch((err) => {
      showToast(localizeErrorMessage(err, 'common.downloadFailed'), 'error');
    });
  }, [fileCtx.fileInfo]);

  // 化名对照表 csv（替换模式执行成功后可用）：用执行响应的 entity_map
  // （后端真实替换结果，含 coref 复用）生成，与成品天然一致
  const handleDownloadPseudonymCsv = useCallback(() => {
    if (!fileCtx.fileInfo || !confirmedPseudonymMap) return;
    const csv = buildPseudonymCsv(entityCtx.entities, entityMap, {
      headers: [
        t('playground.pseudonymCsvColOriginal'),
        t('playground.pseudonymCsvColType'),
        t('playground.pseudonymCsvColReplacement'),
        t('playground.pseudonymCsvColCount'),
      ],
      typeLabel: (type) => recognition.getTypeConfig(type)?.name ?? type,
    });
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const base = fileCtx.fileInfo.filename.replace(/\.[^.]+$/, '');
    triggerDownload(blob, `${t('playground.pseudonymCsvFilePrefix')}_${base}.csv`);
  }, [confirmedPseudonymMap, entityCtx.entities, entityMap, fileCtx.fileInfo, recognition]);

  const openPopout = useCallback(() => {
    imageCtx.openPopout(recognition.visionTypes);
  }, [imageCtx, recognition.visionTypes]);

  return {
    stage: fileCtx.stage,
    setStage: fileCtx.setStage,
    fileInfo: fileCtx.fileInfo,
    content: fileCtx.content,
    isImageMode: fileCtx.isImageMode,
    entities: entityCtx.entities,
    setEntities: entityCtx.setEntities,
    applyEntities: entityCtx.applyEntities,
    boundingBoxes: imageCtx.boundingBoxes,
    setBoundingBoxes: imageCtx.setBoundingBoxes,
    visibleBoxes: imageCtx.visibleBoxes,
    isLoading: fileCtx.isLoading,
    loadingMessage: fileCtx.loadingMessage,
    visionStream: fileCtx.visionStream,
    taskRecognition: fileCtx.taskRecognition,
    taskPerformance: fileCtx.taskPerformance,
    taskStageLabel: fileCtx.taskStageLabel,
    uploadIssue: fileCtx.uploadIssue,
    recognitionIssue: fileCtx.recognitionIssue,
    entityMap,
    redactedCount,
    processingMode: recognition.processingMode,
    setProcessingMode: recognition.setProcessingMode,
    pseudonymMap,
    setPseudonymReplacement,
    pseudonymMapLoading,
    pseudonymMapError,
    retryPseudonymLoad,
    replaceUnready,
    pseudonymConflicts,
    confirmedPseudonymMap,
    handleDownloadPseudonymCsv,
    redactionReport,
    reportOpen,
    setReportOpen,
    versionHistory,
    versionHistoryOpen,
    setVersionHistoryOpen,
    selectedCount: historyCtx.selectedCount,
    canUndo: historyCtx.canUndo,
    canRedo: historyCtx.canRedo,
    handleUndo: historyCtx.handleUndo,
    handleRedo: historyCtx.handleRedo,
    entityHistory: entityCtx.entityHistory,
    imageHistory: imageCtx.imageHistory,
    selectAll: historyCtx.selectAll,
    deselectAll: historyCtx.deselectAll,
    toggleBox: imageCtx.toggleBox,
    removeEntity: entityCtx.removeEntity,
    handleRerunNer,
    handleRedact,
    cancelProcessing,
    resumeFromFile,
    handleReset,
    resetConfirmOpen,
    confirmReset,
    cancelReset,
    handleDownload,
    dropzone: fileCtx.dropzone,
    encryptedPrompt: fileCtx.encryptedPrompt,
    handleDecrypted: fileCtx.handleDecrypted,
    clearEncryptedPrompt: fileCtx.clearEncryptedPrompt,
    imageUrl: imageCtx.imageUrl,
    staticPageUrl: imageCtx.staticPageUrl,
    redactedImageUrl: imageCtx.redactedImageUrl,
    redactedImageError: imageCtx.redactedImageError,
    currentPage: imageCtx.currentPage,
    setCurrentPage: imageCtx.setCurrentPage,
    totalPages: imageCtx.totalPages,
    mergeVisibleBoxes: imageCtx.mergeVisibleBoxes,
    openPopout,
    recognition,
  };
}
