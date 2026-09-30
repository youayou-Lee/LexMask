// Copyright 2026 LexMask Contributors

import { startTransition, useState, useCallback, useEffect, useRef } from 'react';
import { useDropzone, type FileRejection } from 'react-dropzone';
import { authFetch } from '@/services/api-client';
import { showToast } from '@/components/Toast';
import { t } from '@/i18n';
import { localizeErrorMessage } from '@/utils/localizeError';
import { ACCEPTED_UPLOAD_FILE_TYPES } from '@/utils/fileUploadAccept';
import { safeJson, runVisionDetectionPages } from '../utils';
import { planServerCachedResume } from '../lib/playground-draft';
import type {
  FileInfo,
  Entity,
  BoundingBox,
  Stage,
  UploadResponse,
  ParseResponse,
  NerResponse,
} from '../types';

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
  const [fileInfo, setFileInfo] = useState<FileInfo | null>(null);
  const [content, setContent] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const [loadingMessage, setLoadingMessage] = useState('');
  const [uploadIssue, setUploadIssue] = useState<string | null>(null);
  const [recognitionIssue, setRecognitionIssue] = useState<string | null>(null);
  // Issue #30：解析返回 PDF_ENCRYPTED_NEEDS_PASSWORD 时挂起流程，弹密码框
  const [encryptedPrompt, setEncryptedPrompt] = useState<EncryptedPdfPrompt | null>(null);

  const [pendingFile, setPendingFile] = useState<PendingFile | null>(null);

  const abortRef = useRef<AbortController | null>(null);

  const isImageMode = !!fileInfo && (fileInfo.file_type === 'image' || !!fileInfo.is_scanned);

  // --- Cleanup abort on unmount ---
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
    };
  }, []);

  const cancelProcessing = useCallback((notify = true) => {
    abortRef.current?.abort();
    abortRef.current = null;
    setPendingFile(null);
    setIsLoading(false);
    setLoadingMessage('');
    setRecognitionIssue(null);
    if (notify) {
      showToast(t('playground.cancelled'), 'info');
    }
  }, []);

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
    },
    [],
  );

  // --- File upload ---
  const handleFileDrop = useCallback(async (acceptedFiles: File[]) => {
    if (acceptedFiles.length === 0) return;
    const file = acceptedFiles[0];
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
          setEncryptedPrompt({ fileId: uploadData.file_id, filename: uploadData.filename });
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
  }, [applyParsedFile]);

  // 从服务端按 file_id 重建会话（历史页「回到现场」且无草稿时的 R2 路径）：
  // 后端识别成功后会把 entities + 当时的识别配置写进文件记录，先取缓存直接
  // 恢复「当时的现场」；从未识别过（实体为空，含扫描件）才 parse + 重新识别。
  const loadExistingFile = useCallback(async (fileId: string) => {
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
        opts.setEntities(
          cached.entities.map((e, idx) => ({
            ...e,
            id: (e.id as string | undefined) || `entity_${idx}`,
            selected: (e.selected as boolean | undefined) ?? true,
            source: (e.source as Entity['source'] | undefined) || 'llm',
          })) as Entity[],
        );
        opts.resetEntityHistory();
        if (cached.entityTypeIds) opts.setSelectedTypes(cached.entityTypeIds);
        setStage('preview');
        setIsLoading(false);
        setLoadingMessage('');
        showToast(t('playground.restoredFromServer'), 'info');
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
  }, []);

  // --- Auto-recognition after upload ---
  useEffect(() => {
    if (!pendingFile) return;
    const { fileId, fileType, isScanned, pageCount, content: parsedContent } = pendingFile;
    setPendingFile(null);

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;

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

    const doRecognition = async () => {
      try {
        setRecognitionIssue(null);
        const isImage = fileType === 'image' || isScanned;
        if (isImage) {
          const ocrTypes = opts.latestOcrHasTypesRef.current;
          const visualFeatureTypes = opts.latestVisualFeatureTypesRef.current;
          if (ocrTypes.length === 0 && visualFeatureTypes.length === 0) {
            opts.setBoundingBoxes([]);
            opts.resetImageHistory();
            setStage('preview');
            return;
          }
          const vLabel =
            ocrTypes.length > 0 && visualFeatureTypes.length > 0
              ? t('playground.loading.visionHybrid')
              : ocrTypes.length > 0
                ? t('playground.loading.visionOcr')
                : visualFeatureTypes.length > 0
                  ? t('playground.loading.visionImage')
                  : t('playground.loading.vision');
          setLoadingMessage(vLabel);

          opts.setBoundingBoxes([]);
          opts.resetImageHistory();
          const totalPages = Math.max(1, pageCount);
          const { totalBoxes } = await runVisionDetectionPages({
            fileId,
            ocrHasTypes: ocrTypes,
            visualFeatureTypes,
            totalPages,
            signal,
            label: vLabel,
            setLoadingMessage,
            onPageComplete: ({ pageBoxes }) => {
              startTransition(() => {
                opts.setBoundingBoxes((prev) => [...prev, ...pageBoxes]);
              });
            },
          });
          if (signal.aborted) return;
          showToast(
            t('playground.toast.detectedRegions').replace('{count}', String(totalBoxes)),
            'success',
          );
        } else if (parsedContent) {
          setLoadingMessage(t('playground.loading.text'));
          const nerRes = await authFetch(`/api/v1/files/${fileId}/ner/hybrid`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ entity_type_ids: opts.latestSelectedTypesRef.current }),
            signal,
          });
          if (signal.aborted) return;

          if (!nerRes.ok) {
            throw new Error(t('playground.recognizeFailed'));
          }

          const nerData = await safeJson<NerResponse>(nerRes);
          const entitiesWithSource = (nerData.entities || []).map(
            (e: Record<string, unknown>, idx: number) =>
              ({
                ...e,
                id: e.id || `entity_${idx}`,
                selected: true,
                source: e.source || 'llm',
              }) as Entity,
          );
          opts.setEntities(entitiesWithSource);
          opts.resetEntityHistory();
          showToast(
            t('playground.toast.detectedEntities').replace(
              '{count}',
              String(entitiesWithSource.length),
            ),
            'success',
          );
      }
      if (signal.aborted) return;
      setStage('preview');
    } catch (err) {
      if (signal.aborted) return;
      const message = localizeErrorMessage(err, 'playground.recognizeFailed');
      setRecognitionIssue(message);
      setStage('preview');
      showToast(message, 'error');
    } finally {
      if (!signal.aborted) {
        setIsLoading(false);
        setLoadingMessage('');
        }
      }
    };

    doRecognition();
  }, [pendingFile]);

  // --- Issue #30：密码解密成功后重跑 parse → 自动识别 ---
  const handleDecrypted = useCallback(
    async (fileId: string, filename: string) => {
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
        applyParsedFile(fileId, filename, 0, parseData);
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
    cancelProcessing,
    loadExistingFile,
    uploadIssue,
    recognitionIssue,
    setRecognitionIssue,
    isImageMode,
    dropzone,
    encryptedPrompt,
    handleDecrypted,
    clearEncryptedPrompt,
  };
}
