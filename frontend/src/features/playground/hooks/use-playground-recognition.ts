// Copyright 2026 LexMask Contributors

import { useCallback, useEffect, useMemo, useRef, useState, type SetStateAction } from 'react';
import {
  presetAppliesText,
  presetAppliesVision,
  type RecognitionPreset,
} from '@/services/presetsApi';
import { usePresets } from '@/services/hooks/use-presets';
import {
  fetchRecognitionEntityTypes,
  fetchRecognitionPipelines,
  // Issue#267：目录超时族单一来源（权威定义在服务层；原本地 25s 定义并入，门⑥ M-2）
  RECOGNITION_FETCH_TIMEOUT_MS,
} from '@/services/recognition-config';
import {
  buildDefaultPipelineTypeIds,
  buildDefaultTextTypeIds,
} from '@/services/defaultRedactionPreset';
import {
  buildVisionSelectionSignature,
  getCachedRecognitionConfig,
  updateRecognitionConfigCache,
} from '../lib/recognition-config';
import {
  setActivePresetTextId,
  setActivePresetVisionId,
  getActivePresetTextId,
  getActivePresetVisionId,
} from '@/services/activePresetBridge';
import { t, useI18n } from '@/i18n';
import { STORAGE_KEYS } from '@/constants/storage-keys';
import { getScopedStorageItem, setScopedStorageItem } from '@/lib/storage';
import { useAuth } from '@/features/auth/auth-context';
import { localizePresetName } from '@/features/settings/lib/redaction-display';
import {
  type ConfigLoadState,
  flattenVisionTypes,
  normalizeVisionPipelines,
  sortEntityTypes,
} from '../lib/recognition-config';
import type { EntityTypeConfig, VisionTypeConfig, PipelineConfig } from '../types';

// Issue#267：目录接口超时族单一来源在服务层（recognition-config.ts，本文件 import）。
// 原本地 25s 裁决注释（与批量向导对齐、1.2s 隧道间歇 abort）随定义一并下沉。

function uniqueIds(ids: string[]): string[] {
  return Array.from(new Set(ids));
}

function resolveVisionSelectionsFromStorage(pipelines: PipelineConfig[], ownerId?: string | null) {
  const ocrHasTypeIds = pipelines
    .filter((pipeline) => pipeline.mode === 'ocr_has')
    .flatMap((pipeline) => pipeline.types.map((type) => type.id));
  const defaultOcrHasTypeIds = buildDefaultPipelineTypeIds(pipelines, 'ocr_has');
  const visualFeatureTypeIds = pipelines
    .filter((pipeline) => pipeline.mode === 'visual_features')
    .flatMap((pipeline) => pipeline.types.map((type) => type.id));
  const defaultVisualFeatureTypeIds = buildDefaultPipelineTypeIds(pipelines, 'visual_features');

  const visionSelectionSignature = buildVisionSelectionSignature(pipelines);
  const savedOcrHasTypes = getScopedStorageItem<string[] | null>(
    STORAGE_KEYS.OCR_HAS_TYPES,
    null,
    ownerId,
  );
  const savedVisualFeatureTypes = getScopedStorageItem<string[] | null>(
    STORAGE_KEYS.VISUAL_FEATURE_TYPES,
    null,
    ownerId,
  );
  const savedVisionSelectionSignature = getScopedStorageItem<string | null>(
    STORAGE_KEYS.VISION_SELECTION_SIGNATURE,
    null,
    ownerId,
  );
  const canUseSavedVisionSelection = savedVisionSelectionSignature === visionSelectionSignature;

  const ocrHasTypes =
    canUseSavedVisionSelection && Array.isArray(savedOcrHasTypes)
      ? (() => {
          const filtered = savedOcrHasTypes.filter((id: string) => ocrHasTypeIds.includes(id));
          return filtered.length > 0 || savedOcrHasTypes.length === 0
            ? filtered
            : defaultOcrHasTypeIds;
        })()
      : defaultOcrHasTypeIds;

  const visualFeatureTypes =
    canUseSavedVisionSelection && Array.isArray(savedVisualFeatureTypes)
      ? (() => {
          const filtered = uniqueIds(savedVisualFeatureTypes).filter((id: string) =>
            visualFeatureTypeIds.includes(id),
          );
          return filtered.length > 0 || savedVisualFeatureTypes.length === 0
            ? filtered
            : defaultVisualFeatureTypeIds;
        })()
      : defaultVisualFeatureTypeIds;

  return {
    ocrHasTypes,
    visualFeatureTypes,
    visionSelectionSignature,
  };
}

export function usePlaygroundRecognition() {
  const locale = useI18n((state) => state.locale);
  const { status } = useAuth();
  const ownerKey =
    status?.authenticated && status.username ? status.username.toLowerCase() : 'anonymous';
  const presetsQuery = usePresets();

  const cachedConfig = getCachedRecognitionConfig(ownerKey);
  const cachedEntityTypes = cachedConfig ? sortEntityTypes(cachedConfig.entityTypes) : [];
  const cachedPipelines = cachedConfig
    ? normalizeVisionPipelines(cachedConfig.pipelines as PipelineConfig[])
    : [];
  const cachedVisionSelections = resolveVisionSelectionsFromStorage(cachedPipelines, ownerKey);

  const [entityTypes, setEntityTypes] = useState<EntityTypeConfig[]>(cachedEntityTypes);
  const [textConfigState, setTextConfigState] = useState<ConfigLoadState>(
    cachedEntityTypes.length > 0 ? 'ready' : 'loading',
  );
  const entityConfigLoadedRef = useRef(cachedEntityTypes.length > 0);
  const initialSelectedTypes = buildDefaultTextTypeIds(cachedEntityTypes);
  const [selectedTypes, setSelectedTypesState] = useState<string[]>(initialSelectedTypes);
  const selectedTypesRef = useRef<string[]>(initialSelectedTypes);
  const setSelectedTypes = useCallback((next: SetStateAction<string[]>) => {
    const base = selectedTypesRef.current;
    const resolved = typeof next === 'function' ? next(base) : next;
    selectedTypesRef.current = resolved;
    setSelectedTypesState(resolved);
  }, []);
  const [visionTypes, setVisionTypes] = useState<VisionTypeConfig[]>(() =>
    flattenVisionTypes(cachedPipelines),
  );
  const [visionConfigState, setVisionConfigState] = useState<ConfigLoadState>(
    cachedPipelines.length > 0 ? 'ready' : 'loading',
  );
  const visionConfigLoadedRef = useRef(cachedPipelines.length > 0);
  const [selectedOcrHasTypes, setSelectedOcrHasTypes] = useState<string[]>(() => [
    ...cachedVisionSelections.ocrHasTypes,
  ]);
  const [selectedVisualFeatureTypes, setSelectedVisualFeatureTypes] = useState<string[]>(() => [
    ...cachedVisionSelections.visualFeatureTypes,
  ]);
  const selectedOcrHasTypesRef = useRef(selectedOcrHasTypes);
  const selectedVisualFeatureTypesRef = useRef(selectedVisualFeatureTypes);
  const [pipelines, setPipelines] = useState<PipelineConfig[]>(cachedPipelines);
  // 处理方式上层二选一：打码（默认，走 replacementMode 三选一）/ 替换（化名映射确认）
  const [processingMode, setProcessingMode] = useState<'mask' | 'replace'>('mask');
  const [replacementMode, setReplacementModeState] = useState<
    'structured' | 'smart' | 'mask' | 'pseudonym'
  >('structured');
  // 统一入口：pseudonym 归入替换分支；其余三值落在打码分支作为子模式。
  // 预设应用（含 pseudonym）也走这里，自动映射到对应分支。
  const setReplacementMode = useCallback((mode: 'structured' | 'smart' | 'mask' | 'pseudonym') => {
    if (mode === 'pseudonym') {
      setProcessingMode('replace');
      return;
    }
    setProcessingMode('mask');
    setReplacementModeState(mode);
  }, []);
  // 切回「打码」时清掉残留的 pseudonym 子模式（预设带入），保证打码分支
  // 永远以三种打码方式之一执行，不会以 pseudonym 模式产出化名成品
  const setProcessingModeGuarded = useCallback((mode: 'mask' | 'replace') => {
    setProcessingMode(mode);
    if (mode === 'mask')
      setReplacementModeState((current) => (current === 'pseudonym' ? 'structured' : current));
  }, []);
  // 成品水印文案（W2-1）：只作用于最终执行输出，预览不加
  const [watermarkText, setWatermarkText] = useState('');
  // Issue#244 任务 C：交付水印选项——保留原水印（默认）/ 去除水印（识别前摘除）。
  // 只对打码（mask）模式有意义；替换（structured/pseudonym）产物是文本，无水印面，
  // 提交侧会忽略该值且面板隐藏选项。
  const [watermarkDelivery, setWatermarkDelivery] = useState<'keep' | 'strip'>('keep');
  const [playgroundPresets, setPlaygroundPresets] = useState<RecognitionPreset[]>([]);
  const [playgroundPresetTextId, setPlaygroundPresetTextId] = useState<string | null>(null);
  const [playgroundPresetVisionId, setPlaygroundPresetVisionId] = useState<string | null>(null);
  const [presetApplySeq, setPresetApplySeq] = useState(0);

  useEffect(() => {
    selectedTypesRef.current = selectedTypes;
  }, [selectedTypes]);

  const localizedPlaygroundPresets = useMemo(() => {
    void locale;
    return playgroundPresets.map((preset) => ({
      ...preset,
      name: localizePresetName(preset, t),
    }));
  }, [playgroundPresets, locale]);

  const textPresetsPg = useMemo(
    () => localizedPlaygroundPresets.filter(presetAppliesText),
    [localizedPlaygroundPresets],
  );
  const visionPresetsPg = useMemo(
    () => localizedPlaygroundPresets.filter(presetAppliesVision),
    [localizedPlaygroundPresets],
  );

  const playgroundDefaultTextTypeIds = useMemo(
    () => buildDefaultTextTypeIds(entityTypes),
    [entityTypes],
  );
  const playgroundDefaultOcrHasTypeIds = useMemo(
    () => buildDefaultPipelineTypeIds(pipelines, 'ocr_has'),
    [pipelines],
  );
  const playgroundDefaultVisualFeatureTypeIds = useMemo(
    () => buildDefaultPipelineTypeIds(pipelines, 'visual_features'),
    [pipelines],
  );

  const updateOcrHasTypes = useCallback(
    (types: string[]) => {
      selectedOcrHasTypesRef.current = types;
      setSelectedOcrHasTypes(types);
      setScopedStorageItem(STORAGE_KEYS.OCR_HAS_TYPES, types, ownerKey);
    },
    [ownerKey],
  );

  const updateVisualFeatureTypes = useCallback(
    (types: string[]) => {
      const uniqueTypes = uniqueIds(types);
      selectedVisualFeatureTypesRef.current = uniqueTypes;
      setSelectedVisualFeatureTypes(uniqueTypes);
      setScopedStorageItem(STORAGE_KEYS.VISUAL_FEATURE_TYPES, uniqueTypes, ownerKey);
    },
    [ownerKey],
  );

  const clearPlaygroundTextPresetTracking = useCallback(() => {
    setPlaygroundPresetTextId(null);
    setActivePresetTextId(null);
  }, []);

  const clearPlaygroundVisionPresetTracking = useCallback(() => {
    setPlaygroundPresetVisionId(null);
    setActivePresetVisionId(null);
  }, []);

  const applyTextPresetToPlayground = useCallback(
    (preset: RecognitionPreset) => {
      if (!presetAppliesText(preset)) return;
      const enabledTextIds = new Set(
        entityTypes.filter((type) => type.enabled !== false).map((type) => type.id),
      );
      setSelectedTypes(preset.selectedEntityTypeIds.filter((id) => enabledTextIds.has(id)));
      if ((preset.kind ?? 'full') !== 'text') {
        setReplacementMode(preset.replacementMode);
      }
      setPlaygroundPresetTextId(preset.id);
      setActivePresetTextId(preset.id);
      setPresetApplySeq((s) => s + 1);
    },
    [entityTypes, setSelectedTypes, setReplacementMode],
  );

  const applyVisionPresetToPlayground = useCallback(
    (preset: RecognitionPreset) => {
      if (!presetAppliesVision(preset)) return;
      const hasLoadedPipelines = pipelines.length > 0;
      const ocrIds = hasLoadedPipelines
        ? pipelines
            .filter((pipeline) => pipeline.mode === 'ocr_has')
            .flatMap((pipeline) => pipeline.types.map((type) => type.id))
        : null;
      const imageIds = hasLoadedPipelines
        ? pipelines
            .filter((pipeline) => pipeline.mode === 'visual_features')
            .flatMap((pipeline) => pipeline.types.map((type) => type.id))
        : null;

      updateOcrHasTypes(
        hasLoadedPipelines
          ? preset.ocrHasTypes.filter((id) => ocrIds?.includes(id))
          : [...preset.ocrHasTypes],
      );
      updateVisualFeatureTypes(
        hasLoadedPipelines
          ? uniqueIds(preset.visualFeatureTypes ?? []).filter((id) => imageIds?.includes(id))
          : uniqueIds(preset.visualFeatureTypes ?? []),
      );
      setPlaygroundPresetVisionId(preset.id);
      setActivePresetVisionId(preset.id);
      setPresetApplySeq((s) => s + 1);
    },
    [pipelines, updateOcrHasTypes, updateVisualFeatureTypes],
  );

  const selectPlaygroundTextPresetById = useCallback(
    (id: string) => {
      if (!id) {
        setPlaygroundPresetTextId(null);
        setActivePresetTextId(null);
        setSelectedTypes([...playgroundDefaultTextTypeIds]);
        setReplacementMode('structured');
        setPresetApplySeq((s) => s + 1);
        return;
      }

      const preset = playgroundPresets.find((item) => item.id === id);
      if (preset) applyTextPresetToPlayground(preset);
    },
    [
      playgroundDefaultTextTypeIds,
      playgroundPresets,
      applyTextPresetToPlayground,
      setSelectedTypes,
      setReplacementMode,
    ],
  );

  const selectPlaygroundVisionPresetById = useCallback(
    (id: string) => {
      if (!id) {
        setPlaygroundPresetVisionId(null);
        setActivePresetVisionId(null);
        updateOcrHasTypes([...playgroundDefaultOcrHasTypeIds]);
        updateVisualFeatureTypes([...playgroundDefaultVisualFeatureTypeIds]);
        setPresetApplySeq((s) => s + 1);
        return;
      }

      const preset = playgroundPresets.find((item) => item.id === id);
      if (preset) applyVisionPresetToPlayground(preset);
    },
    [
      playgroundDefaultOcrHasTypeIds,
      playgroundDefaultVisualFeatureTypeIds,
      playgroundPresets,
      applyVisionPresetToPlayground,
      updateOcrHasTypes,
      updateVisualFeatureTypes,
    ],
  );

  // Sync presets from react-query cache into local state
  useEffect(() => {
    setPlaygroundPresets(presetsQuery.data ?? []);
  }, [presetsQuery.data]);

  const fetchEntityTypes = useCallback(
    async (preserveSelection = false) => {
      try {
        const types = sortEntityTypes(
          await fetchRecognitionEntityTypes(true, RECOGNITION_FETCH_TIMEOUT_MS),
        );
        const defaultTypeIds = buildDefaultTextTypeIds(types);
        const validTypeIds = new Set(types.map((type) => type.id));
        const hadLoaded = entityConfigLoadedRef.current;

        setEntityTypes(types);
        entityConfigLoadedRef.current = types.length > 0;
        setTextConfigState(types.length > 0 ? 'ready' : 'empty');
        setSelectedTypes((previous) => {
          if (!preserveSelection || !hadLoaded) return defaultTypeIds;
          const filtered = previous.filter((id) => validTypeIds.has(id));
          return filtered.length > 0 || previous.length === 0 ? filtered : defaultTypeIds;
        });
        updateRecognitionConfigCache({ entityTypes: types }, ownerKey);
      } catch (error) {
        if (import.meta.env.DEV) console.error('fetch entity types failed', error);
        if (!entityConfigLoadedRef.current) {
          setTextConfigState('unavailable');
        }
      }
    },
    [ownerKey, setSelectedTypes],
  );

  const fetchVisionTypes = useCallback(
    async (preserveSelection = false) => {
      try {
        const normalizedPipelines = normalizeVisionPipelines(
          (await fetchRecognitionPipelines(RECOGNITION_FETCH_TIMEOUT_MS)) as PipelineConfig[],
        );
        const nextVisionTypes = flattenVisionTypes(normalizedPipelines);
        const hadLoaded = visionConfigLoadedRef.current;

        setPipelines(normalizedPipelines);
        setVisionTypes(nextVisionTypes);
        visionConfigLoadedRef.current = normalizedPipelines.length > 0;
        setVisionConfigState(normalizedPipelines.length > 0 ? 'ready' : 'empty');
        if (preserveSelection && hadLoaded) {
          // A refetch (window focus / config-changed event) must NOT clobber the
          // user's current selection (e.g. an applied preset). Mirror
          // fetchEntityTypes: keep the live selection, only dropping ids that no
          // longer exist in the pipelines. Re-resolving from storage here is what
          // caused the re-recognize 13<->7 flip (a focus refetch reset an applied
          // medical preset back to the default type set).
          const ocrIds = new Set(
            normalizedPipelines
              .filter((pipeline) => pipeline.mode === 'ocr_has')
              .flatMap((pipeline) => pipeline.types.map((type) => type.id)),
          );
          const imageIds = new Set(
            normalizedPipelines
              .filter((pipeline) => pipeline.mode === 'visual_features')
              .flatMap((pipeline) => pipeline.types.map((type) => type.id)),
          );
          updateOcrHasTypes(selectedOcrHasTypesRef.current.filter((id) => ocrIds.has(id)));
          updateVisualFeatureTypes(
            selectedVisualFeatureTypesRef.current.filter((id) => imageIds.has(id)),
          );
        } else {
          const nextVisionSelections = resolveVisionSelectionsFromStorage(
            normalizedPipelines,
            ownerKey,
          );
          updateOcrHasTypes(nextVisionSelections.ocrHasTypes);
          updateVisualFeatureTypes(nextVisionSelections.visualFeatureTypes);
          setScopedStorageItem(
            STORAGE_KEYS.VISION_SELECTION_SIGNATURE,
            nextVisionSelections.visionSelectionSignature,
            ownerKey,
          );
        }
        updateRecognitionConfigCache({ pipelines: normalizedPipelines }, ownerKey);
      } catch (error) {
        if (import.meta.env.DEV) console.error('fetch vision pipelines failed', error);
        if (!visionConfigLoadedRef.current) {
          setVisionConfigState('unavailable');
        }
      }
    },
    [ownerKey, updateOcrHasTypes, updateVisualFeatureTypes],
  );

  const loadRecognitionConfig = useCallback(
    async (preserveSelection = false) => {
      await Promise.allSettled([
        fetchEntityTypes(preserveSelection),
        fetchVisionTypes(preserveSelection),
      ]);
    },
    [fetchEntityTypes, fetchVisionTypes],
  );

  useEffect(() => {
    void loadRecognitionConfig(false);
  }, [loadRecognitionConfig]);

  useEffect(() => {
    const handleFocus = () => {
      void loadRecognitionConfig(true);
    };

    window.addEventListener('focus', handleFocus);
    const handleEntityTypesChanged = () => fetchEntityTypes(true);
    window.addEventListener('entity-types-changed', handleEntityTypesChanged);
    return () => {
      window.removeEventListener('focus', handleFocus);
      window.removeEventListener('entity-types-changed', handleEntityTypesChanged);
    };
  }, [fetchEntityTypes, loadRecognitionConfig]);

  const bridgeInitRef = useRef(false);
  useEffect(() => {
    bridgeInitRef.current = false;
  }, [ownerKey]);

  useEffect(() => {
    if (bridgeInitRef.current) return;
    if (!playgroundPresets.length || !entityTypes.length) return;

    const textPresetId = getActivePresetTextId();
    if (textPresetId) {
      const preset = playgroundPresets.find(
        (item) => item.id === textPresetId && presetAppliesText(item),
      );
      if (preset) applyTextPresetToPlayground(preset);
    }

    const visionPresetId = getActivePresetVisionId();
    if (visionPresetId && pipelines.length) {
      const preset = playgroundPresets.find(
        (item) => item.id === visionPresetId && presetAppliesVision(item),
      );
      if (preset) applyVisionPresetToPlayground(preset);
    }

    bridgeInitRef.current = true;
  }, [
    playgroundPresets,
    entityTypes,
    pipelines,
    applyTextPresetToPlayground,
    applyVisionPresetToPlayground,
  ]);

  const getTypeConfig = useCallback(
    (typeId: string): { name: string; color: string } => {
      const config = entityTypes.find((type) => type.id === typeId);
      return config || { name: typeId, color: '#6366F1' };
    },
    [entityTypes],
  );

  const getVisionTypeConfig = useCallback(
    (typeId: string): { name: string; color: string } => {
      const config = visionTypes.find((type) => type.id === typeId);
      return config || { name: typeId, color: '#6366F1' };
    },
    [visionTypes],
  );

  const toggleVisionType = useCallback(
    (typeId: string, pipelineMode: 'ocr_has' | 'visual_features') => {
      clearPlaygroundVisionPresetTracking();
      if (pipelineMode === 'ocr_has') {
        const isActive = selectedOcrHasTypes.includes(typeId);
        const next = isActive
          ? selectedOcrHasTypes.filter((id) => id !== typeId)
          : [...selectedOcrHasTypes, typeId];
        updateOcrHasTypes(next);
        return { typeId, wasActive: isActive };
      }
      const isActive = selectedVisualFeatureTypes.includes(typeId);
      const next = isActive
        ? selectedVisualFeatureTypes.filter((id) => id !== typeId)
        : [...selectedVisualFeatureTypes, typeId];
      updateVisualFeatureTypes(next);
      return { typeId, wasActive: isActive };
    },
    [
      selectedOcrHasTypes,
      selectedVisualFeatureTypes,
      updateOcrHasTypes,
      updateVisualFeatureTypes,
      clearPlaygroundVisionPresetTracking,
    ],
  );

  return {
    entityTypes,
    textConfigState,
    // Issue#267：弹窗 unavailable 区重试按钮的目标——重取两目录；调用方必须传
    // preserveSelection=true（已就绪维度过滤保留，未就绪维度走默认集），传 false 会
    // 把已就绪维度的用户选择重置为默认集。与 window focus 自动重取同口径。
    loadRecognitionConfig,
    selectedTypes,
    selectedTypesRef,
    setSelectedTypes,
    visionTypes,
    visionConfigState,
    selectedOcrHasTypes,
    selectedVisualFeatureTypes,
    selectedOcrHasTypesRef,
    selectedVisualFeatureTypesRef,
    pipelines,
    processingMode,
    setProcessingMode: setProcessingModeGuarded,
    replacementMode,
    setReplacementMode,
    watermarkText,
    setWatermarkText,
    watermarkDelivery,
    setWatermarkDelivery,
    textPresetsPg,
    visionPresetsPg,
    playgroundPresetTextId,
    playgroundPresetVisionId,
    selectPlaygroundTextPresetById,
    selectPlaygroundVisionPresetById,
    clearPlaygroundTextPresetTracking,
    clearPlaygroundVisionPresetTracking,
    toggleVisionType,
    updateOcrHasTypes,
    updateVisualFeatureTypes,
    presetApplySeq,
    getTypeConfig,
    getVisionTypeConfig,
  };
}
