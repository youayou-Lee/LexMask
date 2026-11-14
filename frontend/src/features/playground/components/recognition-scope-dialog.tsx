// Copyright 2026 LexMask Contributors

import { type FC, useEffect, useMemo, useState } from 'react';
import { FileSearch, ScanText, Stamp } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { useServiceHealth, type ServiceInfo } from '@/hooks/use-service-health';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { resolveLegalTextSelection, sameIdSelection } from '@/config/legalPreset';
import { buildDefaultPipelineTypeIds } from '@/services/defaultRedactionPreset';
import { createPreset, presetAppliesText, presetAppliesVision } from '@/services/presetsApi';
import { useInvalidatePresets, usePresets } from '@/services/hooks/use-presets';
import { localizeRecognitionTypeName } from '@/features/settings/lib/redaction-display';
import { localizeErrorMessage } from '@/utils/localizeError';
import { showToast } from '@/components/Toast';
import { ConfigEmptyState, dedupeRecognitionTypes, resolveTextTypeName } from './playground-upload-config';
import type { usePlayground } from '../hooks/use-playground';
import type { EntityTypeConfig, VisionTypeConfig } from '../types';

/**
 * Issue#262 二期 Task 10b：识别范围三区弹窗（替换 T6 内联抽屉）。
 *
 * 三区并排（<1100px 纵叠）：ner=语义识别（entityTypes ↔ selectedTypes）、
 * ocr=文字有无检测（ocr_has 管线 ↔ selectedOcrHasTypes）、la=视觉特征定位
 * （visual_features 管线 ↔ selectedVisualFeatureTypes）。每区 8 类/页翻页、
 * 区内全选/全不选、即点即存（直接写 rec 状态，无「保存」步骤）；服务 offline
 * 整区暗化 + 区内红条；纯文本文件 OCR/LA 两区「不适用」（na ≠ 离线）。
 *
 * 方案三态（select 派生值，非独立状态）：legal=法律集+后端默认视觉；all=三清单全量；
 * user=「我的方案」（RecognitionPreset，经 rec.selectPlayground*PresetById 套用，
 * kind 语义与旧 PresetSelectors 一致）；手动改勾 → tracking 清掉 → 落到 custom。
 * 保存为方案 = createPreset(kind:'full'，当前三清单) + invalidatePresets + 自动套用。
 *
 * visionApplicable 由上传页按 ctx.fileInfo 计算（is_scanned || file_type==='image'，
 * 同 use-playground-file.ts 的 isVisionFile 判定）；无文件（上传态常态）传 true——
 * 三区全部可用，与「扫描件预览」口径一致；文本文件记录存在时两区收成 na。
 */

const SCOPE_PAGE_SIZE = 8;

type ZoneId = 'ner' | 'ocr' | 'la';
type RecognitionCtx = ReturnType<typeof usePlayground>['recognition'];
type ServiceStatus = NonNullable<ServiceInfo['status']>;

interface ZoneDef {
  id: ZoneId;
  nameKey: string;
  descKey: string;
  serviceKey: 'has_ner' | 'paddle_ocr' | 'visual_features';
}

const ZONES: ZoneDef[] = [
  { id: 'ner', nameKey: 'playground.scope.zoneNer', descKey: 'playground.scope.zoneNerDesc', serviceKey: 'has_ner' },
  { id: 'ocr', nameKey: 'playground.scope.zoneOcr', descKey: 'playground.scope.zoneOcrDesc', serviceKey: 'paddle_ocr' },
  { id: 'la', nameKey: 'playground.scope.zoneLa', descKey: 'playground.scope.zoneLaDesc', serviceKey: 'visual_features' },
];

const ZONE_ICONS: Record<ZoneId, typeof ScanText> = {
  ner: ScanText,
  ocr: FileSearch,
  la: Stamp,
};

const SCHEME_LEGAL = 'legal';
const SCHEME_ALL = 'all';
const SCHEME_CUSTOM = 'custom';

function statusLabel(status: ServiceStatus, t: (key: string) => string) {
  return t(`health.${status}`);
}

export const RecognitionScopeDialog: FC<{
  open: boolean;
  onOpenChange: (open: boolean) => void;
  rec: RecognitionCtx;
  visionApplicable: boolean;
}> = ({ open, onOpenChange, rec, visionApplicable }) => {
  const t = useT();
  const { health, checking } = useServiceHealth();
  const { data: userPresets = [] } = usePresets();
  const invalidatePresets = useInvalidatePresets();

  // ── 区数据（与旧 TextTypeGroups / VisionPipelines 同源，防同名重复 chip） ──
  const nerTypes = useMemo(
    () => dedupeRecognitionTypes(rec.entityTypes),
    [rec.entityTypes],
  );
  const ocrTypes = useMemo(
    () =>
      dedupeRecognitionTypes(
        rec.pipelines
          .filter((pipeline) => pipeline.mode === 'ocr_has')
          .flatMap((pipeline) => pipeline.types),
      ),
    [rec.pipelines],
  );
  const laTypes = useMemo(
    () =>
      dedupeRecognitionTypes(
        rec.pipelines
          .filter((pipeline) => pipeline.mode === 'visual_features')
          .flatMap((pipeline) => pipeline.types),
      ),
    [rec.pipelines],
  );
  const zoneTypes: Record<ZoneId, (EntityTypeConfig | VisionTypeConfig)[]> = {
    ner: nerTypes,
    ocr: ocrTypes,
    la: laTypes,
  };

  // ── Issue#267：区级目录三态（loading/empty/unavailable）。
  // ner 区目录态取文本目录状态机，ocr/la 区取视觉管线状态机——两状态源独立。
  // 目录态 ≠ ready 时以占位面板替代 chip 墙（ConfigEmptyState 等价复用，原 55002ec
  // 视觉），排在搜索无命中之前：目录不可用时 types 必为空、搜索必然 0 命中，
  // 「目录加载失败」比「无匹配」信息量大。unavailable 面板带重试按钮
  // （loadRecognitionConfig(true)，preserveSelection 防重置已就绪维度的用户选择）。
  const zoneCatalogState = (zone: ZoneDef): Exclude<RecognitionCtx['textConfigState'], 'ready'> | 'ready' =>
    zone.id === 'ner' ? rec.textConfigState : rec.visionConfigState;
  const catalogHintKey = (state: RecognitionCtx['textConfigState']) =>
    state === 'loading'
      ? 'playground.scope.catalogLoading'
      : state === 'empty'
        ? 'playground.scope.catalogEmpty'
        : 'playground.scope.catalogUnavailable';
  // Issue#267：文本目录未就绪时方案 select 与「保存为方案」一并禁用——
  // applyScheme 的 legal/all/user preset 三条路径在空目录下都会写空集（视觉侧已有
  // hasPipelines 对称守卫），单点门控上游堵住，不在 applyScheme 内加第二层守卫。
  const textCatalogReady = rec.textConfigState === 'ready';

  // ── 分页（每区独立；页号超出时渲染侧收敛） ──
  const [zonePages, setZonePages] = useState<Record<ZoneId, number>>({ ner: 1, ocr: 1, la: 1 });

  // ── 全局搜索（Issue#262 二期反馈）：三区同时过滤（显示名 OR id，大小写不敏感）。
  // 搜索态该区不分页（全部命中一次渲染）、翻页控件禁用并显示匹配计数；zonePages 不动，
  // 清空搜索即恢复搜索前页码。只作用于可用区——离线区/na 区不参与搜索展示。
  const [scopeSearch, setScopeSearch] = useState('');
  const scopeQuery = scopeSearch.trim().toLowerCase();

  // ── 服务状态（侧边栏同口径：busy/checking 可用；无健康快照时 fallbackService 语义） ──
  const zoneStatus = (zone: ZoneDef): ServiceStatus => {
    const service = health?.services[zone.serviceKey];
    if (!service) return checking ? 'checking' : 'offline';
    return service.status;
  };
  const isZoneOffline = (zone: ZoneDef) => zoneStatus(zone) === 'offline';
  // na 优先于 offline（预览口径）：纯文本文件不走 OCR/LA 两区，无论服务是否离线
  // 都显示「不适用」；服务徽标仍如实显示离线状态。
  const isZoneNa = (zone: ZoneDef) => zone.id !== 'ner' && !visionApplicable;
  const isZoneActive = (zone: ZoneDef) => !isZoneOffline(zone) && !isZoneNa(zone);

  // ── 方案三态（派生值）：tracking > legal 形态 > all 形态 > custom ──
  const entityTypeIds = useMemo(() => rec.entityTypes.map((type) => type.id), [rec.entityTypes]);
  const legalSelection = useMemo(() => resolveLegalTextSelection(entityTypeIds), [entityTypeIds]);
  const ocrDefaultIds = useMemo(
    () => buildDefaultPipelineTypeIds(rec.pipelines, 'ocr_has'),
    [rec.pipelines],
  );
  const visualDefaultIds = useMemo(
    () => buildDefaultPipelineTypeIds(rec.pipelines, 'visual_features'),
    [rec.pipelines],
  );
  const ocrAllIds = useMemo(() => ocrTypes.map((type) => type.id), [ocrTypes]);
  const visualAllIds = useMemo(() => laTypes.map((type) => type.id), [laTypes]);

  // hook 的 tracking 按维度清（clearPlaygroundTextPresetTracking 只清 text 维、
  // toggleVisionType 只清 vision 维），单看任一维都会在「保存→套用→手动改另一区」
  // 后仍显示方案名而配置已失真。这里要求两维 tracking 指向同一方案（覆盖 full-kind）、
  // 且当前三清单与该方案按应用时同款目录过滤后的期望形态一致，否则落入形态比较/custom。
  const trackedPresetId = (() => {
    const textId = rec.playgroundPresetTextId;
    if (textId === null || textId !== rec.playgroundPresetVisionId) return null;
    const preset = userPresets.find((item) => item.id === textId);
    if (!preset || !presetAppliesText(preset) || !presetAppliesVision(preset)) return null;
    const enabledTextIds = new Set(
      rec.entityTypes.filter((type) => type.enabled !== false).map((type) => type.id),
    );
    if (
      !sameIdSelection(
        rec.selectedTypes,
        preset.selectedEntityTypeIds.filter((id) => enabledTextIds.has(id)),
      )
    ) {
      return null;
    }
    if (!sameIdSelection(rec.selectedOcrHasTypes, preset.ocrHasTypes.filter((id) => ocrAllIds.includes(id)))) {
      return null;
    }
    if (
      !sameIdSelection(
        rec.selectedVisualFeatureTypes,
        (preset.visualFeatureTypes ?? []).filter((id) => visualAllIds.includes(id)),
      )
    ) {
      return null;
    }
    return textId;
  })();
  const schemeValue = (() => {
    if (trackedPresetId) {
      return trackedPresetId;
    }
    if (
      sameIdSelection(rec.selectedTypes, legalSelection) &&
      sameIdSelection(rec.selectedOcrHasTypes, ocrDefaultIds) &&
      sameIdSelection(rec.selectedVisualFeatureTypes, visualDefaultIds)
    ) {
      return SCHEME_LEGAL;
    }
    if (
      sameIdSelection(rec.selectedTypes, entityTypeIds) &&
      sameIdSelection(rec.selectedOcrHasTypes, ocrAllIds) &&
      sameIdSelection(rec.selectedVisualFeatureTypes, visualAllIds)
    ) {
      return SCHEME_ALL;
    }
    return SCHEME_CUSTOM;
  })();

  const applyScheme = (value: string) => {
    if (value === SCHEME_LEGAL) {
      // 视觉侧只在管线目录就绪时写默认集（目录未加载时写空会经存储误清用户选择），
      // 与 rec.applyVisionPresetToPlayground 的 hasLoadedPipelines 守卫同口径。
      const hasPipelines = rec.pipelines.length > 0;
      rec.setSelectedTypes(resolveLegalTextSelection(rec.entityTypes.map((type) => type.id)));
      rec.clearPlaygroundTextPresetTracking();
      if (hasPipelines) {
        rec.updateOcrHasTypes(ocrDefaultIds);
        rec.updateVisualFeatureTypes(visualDefaultIds);
      }
      rec.clearPlaygroundVisionPresetTracking();
      return;
    }
    if (value === SCHEME_ALL) {
      const hasPipelines = rec.pipelines.length > 0;
      rec.setSelectedTypes(rec.entityTypes.map((type) => type.id));
      rec.clearPlaygroundTextPresetTracking();
      if (hasPipelines) {
        rec.updateOcrHasTypes(ocrAllIds);
        rec.updateVisualFeatureTypes(visualAllIds);
      }
      rec.clearPlaygroundVisionPresetTracking();
      return;
    }
    if (value === SCHEME_CUSTOM) return;
    // user preset：复用 rec 既有套用语义（目录过滤/kind 判别/replacementMode/追踪）
    rec.selectPlaygroundTextPresetById(value);
    rec.selectPlaygroundVisionPresetById(value);
  };

  // ── 保存为方案：内联表单 → createPreset(kind full+三清单) → invalidate → 自动套用 ──
  const [saveFormOpen, setSaveFormOpen] = useState(false);
  const [schemeName, setSchemeName] = useState('');
  const [schemeSaving, setSchemeSaving] = useState(false);
  const [pendingSchemeId, setPendingSchemeId] = useState<string | null>(null);

  // 保存成功后等 react-query 刷新同步进 rec 的清单，再经 rec 既有套用路径
  // 自动选中（目录过滤/replacementMode/追踪一次到位）；查不到就继续等，不硬套。
  const {
    textPresetsPg,
    visionPresetsPg,
    selectPlaygroundTextPresetById,
    selectPlaygroundVisionPresetById,
  } = rec;
  useEffect(() => {
    if (!pendingSchemeId) return;
    if (!textPresetsPg.some((preset) => preset.id === pendingSchemeId)) return;
    selectPlaygroundTextPresetById(pendingSchemeId);
    selectPlaygroundVisionPresetById(pendingSchemeId);
    setPendingSchemeId(null);
  }, [
    pendingSchemeId,
    textPresetsPg,
    visionPresetsPg,
    selectPlaygroundTextPresetById,
    selectPlaygroundVisionPresetById,
  ]);

  const confirmSaveScheme = async () => {
    const name = schemeName.trim();
    if (!name) {
      showToast(t('settings.redaction.nameRequired'), 'error');
      return;
    }
    setSchemeSaving(true);
    try {
      const created = await createPreset({
        name,
        kind: 'full',
        selectedEntityTypeIds: rec.selectedTypes,
        ocrHasTypes: rec.selectedOcrHasTypes,
        visualFeatureTypes: rec.selectedVisualFeatureTypes,
        replacementMode: rec.replacementMode,
      });
      await invalidatePresets();
      setPendingSchemeId(created.id);
      setSaveFormOpen(false);
      setSchemeName('');
      showToast(t('playground.scope.schemeSaved').replace('{name}', name), 'success');
    } catch (error) {
      showToast(localizeErrorMessage(error, 'preset.save.failed'), 'error');
    } finally {
      setSchemeSaving(false);
    }
  };

  // ── 即点即存：各区 chip / 全选 / 全不选直接写 rec 状态 ──
  // NER chip 用函数式更新（同旧 TextTypeGroups）：连点两颗 chip 在同一批次内也不丢更新。
  const toggleNerType = (typeId: string, checked: boolean) => {
    rec.clearPlaygroundTextPresetTracking();
    rec.setSelectedTypes((previous: string[]) =>
      checked
        ? previous.filter((id) => id !== typeId)
        : [...previous, typeId],
    );
  };
  const setNerZoneSelection = (ids: string[]) => {
    rec.clearPlaygroundTextPresetTracking();
    rec.setSelectedTypes(ids);
  };
  const setVisionZoneSelection = (
    mode: 'ocr_has' | 'visual_features',
    ids: string[],
  ) => {
    rec.clearPlaygroundVisionPresetTracking();
    if (mode === 'ocr_has') {
      rec.updateOcrHasTypes(ids);
    } else {
      rec.updateVisualFeatureTypes(ids);
    }
  };

  const resolveTypeName = (
    zone: ZoneDef,
    type: EntityTypeConfig | VisionTypeConfig,
  ): string => {
    if (zone.id === 'ner') {
      return resolveTextTypeName(type.id, (type as EntityTypeConfig).name, t);
    }
    return localizeRecognitionTypeName(type as VisionTypeConfig, t);
  };

  // 页脚「已选 n 类」：只数当次识别真正会走的区（可用且适用）。
  const selectedTotal = ZONES.reduce((total, zone) => {
    if (!isZoneActive(zone)) return total;
    if (zone.id === 'ner') return total + rec.selectedTypes.length;
    if (zone.id === 'ocr') return total + rec.selectedOcrHasTypes.length;
    return total + rec.selectedVisualFeatureTypes.length;
  }, 0);

  if (!open) return null;

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-h-[90vh] w-[calc(100vw-2rem)] max-w-[1140px] overflow-y-auto gap-4 p-5"
        data-testid="playground-scope-dialog"
      >
        <DialogHeader className="gap-2.5">
          <DialogTitle className="text-base">{t('playground.scope.title')}</DialogTitle>
          <DialogDescription className="text-xs leading-4">
            {t('playground.scope.subtitle')}
          </DialogDescription>
          <div className="flex flex-wrap items-center gap-2 pt-1">
            <label
              htmlFor="playground-scheme-select"
              className="shrink-0 text-xs font-medium text-muted-foreground"
            >
              {t('playground.scope.schemeLabel')}
            </label>
            <select
              id="playground-scheme-select"
              data-testid="playground-scheme-select"
              value={schemeValue}
              disabled={!textCatalogReady}
              title={!textCatalogReady ? t(catalogHintKey(rec.textConfigState)) : undefined}
              onChange={(event) => applyScheme(event.currentTarget.value)}
              className="h-8 min-w-0 max-w-[16rem] flex-1 rounded-xl border border-input bg-[var(--surface-control)] px-2.5 text-xs shadow-[var(--shadow-control)] outline-none focus-visible:ring-2 focus-visible:ring-[var(--ring)] disabled:cursor-not-allowed disabled:opacity-50 sm:flex-none"
            >
              <option value={SCHEME_LEGAL}>{t('playground.presetLegal')}</option>
              <option value={SCHEME_ALL}>{t('playground.presetAll')}</option>
              {userPresets.length > 0 && (
                <optgroup label={t('playground.scope.schemeMyGroup')}>
                  {userPresets.map((preset) => (
                    <option key={preset.id} value={preset.id}>
                      {preset.name}
                    </option>
                  ))}
                </optgroup>
              )}
              <option value={SCHEME_CUSTOM} disabled>
                {t('playground.scope.schemeCustom')}
              </option>
            </select>
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="h-8 shrink-0 rounded-xl px-3 text-xs"
              data-testid="playground-scheme-save"
              disabled={schemeSaving || !textCatalogReady}
              title={!textCatalogReady ? t(catalogHintKey(rec.textConfigState)) : undefined}
              onClick={() => setSaveFormOpen((current) => !current)}
            >
              {t('playground.scope.schemeSave')}
            </Button>
          </div>
          <Input
            value={scopeSearch}
            onChange={(event) => setScopeSearch(event.target.value)}
            placeholder={t('playground.scope.searchPlaceholder')}
            aria-label={t('playground.scope.searchLabel')}
            data-testid="playground-scope-search"
            className="h-8 w-full rounded-xl text-xs"
          />
          {saveFormOpen && (
            <form
              className="flex flex-wrap items-center gap-2"
              onSubmit={(event) => {
                event.preventDefault();
                void confirmSaveScheme();
              }}
            >
              <Input
                data-testid="playground-scheme-save-input"
                value={schemeName}
                onChange={(event) => setSchemeName(event.target.value)}
                placeholder={t('playground.scope.schemeNamePlaceholder')}
                autoFocus
                className="h-8 min-w-0 flex-1 rounded-xl text-xs sm:max-w-[18rem]"
              />
              <Button
                type="submit"
                size="sm"
                className="h-8 shrink-0 rounded-xl px-3 text-xs"
                data-testid="playground-scheme-save-confirm"
                disabled={schemeSaving}
              >
                {schemeSaving
                  ? t('settings.redaction.processing')
                  : t('playground.scope.schemeSaveConfirm')}
              </Button>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                className="h-8 shrink-0 rounded-xl px-3 text-xs"
                onClick={() => {
                  setSaveFormOpen(false);
                  setSchemeName('');
                }}
                disabled={schemeSaving}
              >
                {t('common.cancel')}
              </Button>
            </form>
          )}
        </DialogHeader>

        <div className="grid grid-cols-1 gap-3 min-[1100px]:grid-cols-3">
          {ZONES.map((zone) => {
            const Icon = ZONE_ICONS[zone.id];
            const status = zoneStatus(zone);
            const offline = status === 'offline';
            const na = isZoneNa(zone);
            const active = isZoneActive(zone);
            const catalogState = zoneCatalogState(zone);
            const types = zoneTypes[zone.id];
            const ids = types.map((type) => type.id);
            const searching = scopeQuery.length > 0;
            const matchedTypes = searching
              ? types.filter(
                  (type) =>
                    resolveTypeName(zone, type).toLowerCase().includes(scopeQuery) ||
                    type.id.toLowerCase().includes(scopeQuery),
                )
              : types;
            const zoneNoMatch = searching && matchedTypes.length === 0;
            const totalPages = Math.max(1, Math.ceil(types.length / SCOPE_PAGE_SIZE));
            const rawPage = zonePages[zone.id] ?? 1;
            const page = Math.min(Math.max(1, rawPage), totalPages);
            const visibleTypes = searching
              ? matchedTypes
              : types.slice((page - 1) * SCOPE_PAGE_SIZE, page * SCOPE_PAGE_SIZE);
            const selectedSet =
              zone.id === 'ner'
                ? rec.selectedTypes
                : zone.id === 'ocr'
                  ? rec.selectedOcrHasTypes
                  : rec.selectedVisualFeatureTypes;

            const toggleChip = (typeId: string, checked: boolean) => {
              if (zone.id === 'ner') {
                toggleNerType(typeId, checked);
              } else {
                rec.toggleVisionType(typeId, zone.id === 'ocr' ? 'ocr_has' : 'visual_features');
              }
            };
            const selectZoneAll = () => {
              if (zone.id === 'ner') setNerZoneSelection(ids);
              else setVisionZoneSelection(zone.id === 'ocr' ? 'ocr_has' : 'visual_features', ids);
            };
            const selectZoneNone = () => {
              if (zone.id === 'ner') setNerZoneSelection([]);
              else setVisionZoneSelection(zone.id === 'ocr' ? 'ocr_has' : 'visual_features', []);
            };

            return (
              <section
                key={zone.id}
                data-testid={`playground-scope-zone-${zone.id}`}
                className={cn(
                  'flex min-h-0 flex-col rounded-2xl border border-border/70 bg-[var(--surface-control)] p-3',
                  // na 优先：纯文本区显示「不适用」文案时不暗化（可读性），离线红态仍由徽标如实呈现
                  offline && !na && 'pointer-events-none opacity-60 grayscale',
                )}
              >
                <div className="flex min-w-0 items-center gap-1.5">
                  <Icon className="size-4 shrink-0 text-muted-foreground" />
                  <h4 className="min-w-0 truncate text-sm font-semibold tracking-[-0.01em] text-foreground">
                    {t(zone.nameKey)}
                  </h4>
                  <span
                    data-testid={`playground-scope-pill-${zone.id}`}
                    className={cn(
                      'ml-auto flex shrink-0 items-center rounded-full border px-2 py-0.5 text-[10px] font-medium leading-3.5',
                      offline
                        ? 'border-[var(--error-border)] bg-[var(--error-surface)] text-[var(--error-foreground)]'
                        : status === 'degraded' || status === 'checking'
                          ? 'border-[var(--warning-border)] bg-[var(--warning-surface)] text-[var(--warning-foreground)]'
                          : 'border-border/70 bg-background/70 text-muted-foreground',
                    )}
                  >
                    {t(`health.service.${zone.serviceKey}`)} · {statusLabel(status, t)}
                  </span>
                </div>
                <p className="mt-1 text-xs leading-4 text-muted-foreground">{t(zone.descKey)}</p>

                {na ? (
                  <div
                    className="mt-2.5 flex flex-1 items-center justify-center rounded-xl border border-dashed border-border/70 bg-muted/20 px-3 py-6 text-center text-xs leading-4 text-muted-foreground"
                    data-testid={`playground-scope-zone-${zone.id}-na`}
                  >
                    {t('playground.scope.zoneNa')}
                  </div>
                ) : offline ? (
                  <div
                    className="mt-2.5 rounded-xl border border-[var(--error-border)] bg-[var(--error-surface)] px-2.5 py-2 text-xs leading-4 text-[var(--error-foreground)]"
                    data-testid={`playground-scope-zone-${zone.id}-alert`}
                  >
                    {t('playground.scope.zoneOffline')}
                  </div>
                ) : catalogState !== 'ready' ? (
                  // Issue#267：目录三态面板——恢复旧 ConfigEmptyState 视觉（55002ec 等价复用），
                  // 排在搜索无命中之前（目录不可用时搜索必然 0 命中，失败态信息量更大）。
                  <ConfigEmptyState
                    title={t(catalogHintKey(catalogState))}
                    testId={`playground-scope-zone-${zone.id}-${catalogState}`}
                  >
                    {catalogState === 'unavailable' && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        className="mt-3 h-7 shrink-0 rounded-xl px-3 text-xs"
                        data-testid={`playground-scope-zone-${zone.id}-retry`}
                        onClick={() => void rec.loadRecognitionConfig(true)}
                      >
                        {t('common.retry')}
                      </Button>
                    )}
                  </ConfigEmptyState>
                ) : zoneNoMatch ? (
                  // 搜索态 0 命中：区框结构不变，chip 墙换成空态文案
                  <div
                    className="mt-2.5 flex flex-1 items-center justify-center rounded-xl border border-dashed border-border/70 bg-muted/20 px-3 py-6 text-center text-xs leading-4 text-muted-foreground"
                    data-testid={`playground-scope-zone-${zone.id}-nomatch`}
                  >
                    {t('playground.scope.noMatch')}
                  </div>
                ) : (
                  <div className="mt-2.5 grid min-h-0 grid-cols-2 gap-1.5">
                    {visibleTypes.map((type) => {
                      const checked = selectedSet.includes(type.id);
                      const name = resolveTypeName(zone, type);
                      return (
                        <button
                          key={type.id}
                          type="button"
                          data-testid={`playground-scope-chip-${type.id}`}
                          aria-pressed={checked}
                          disabled={offline}
                          title={type.description || name}
                          onClick={() => toggleChip(type.id, checked)}
                          className={cn(
                            'flex min-w-0 items-center overflow-hidden rounded-lg border px-2 py-1.5 text-left text-[11px] leading-4 transition-colors',
                            checked
                              ? 'border-foreground/60 bg-accent font-medium text-foreground'
                              : 'border-border/70 bg-background text-muted-foreground hover:border-border hover:text-foreground',
                          )}
                        >
                          <span className="min-w-0 truncate">{name}</span>
                        </button>
                      );
                    })}
                    {Array.from(
                      { length: Math.max(0, SCOPE_PAGE_SIZE - visibleTypes.length) },
                      (_, idx) => (
                        <div key={`pad-${idx}`} aria-hidden className="invisible" />
                      ),
                    )}
                  </div>
                )}
                {/* 页脚按钮常驻（离线/不适用时禁用置灰，布局不跳）；翻页器只随 chip 墙出现 */}
                <div className="mt-auto flex flex-wrap items-center gap-1.5 pt-2.5">
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-6 shrink-0 whitespace-nowrap rounded-full px-2 text-[10px]"
                    data-testid={`playground-scope-zone-${zone.id}-all`}
                    disabled={!active || ids.length === 0}
                    onClick={selectZoneAll}
                  >
                    {t('playground.scope.zoneAll')}
                  </Button>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-6 shrink-0 whitespace-nowrap rounded-full px-2 text-[10px]"
                    data-testid={`playground-scope-zone-${zone.id}-none`}
                    disabled={!active || ids.length === 0}
                    onClick={selectZoneNone}
                  >
                    {t('playground.scope.zoneNone')}
                  </Button>
                  {!na && !offline && catalogState === 'ready' && (
                    <span
                      className="ml-auto flex shrink-0 items-center gap-0.5 text-[10px] tabular-nums text-muted-foreground"
                      data-testid={`playground-scope-zone-${zone.id}-pager`}
                    >
                      <button
                        type="button"
                        aria-label={t('playground.scope.prevPage')}
                        data-testid={`playground-scope-zone-${zone.id}-page-prev`}
                        disabled={searching || page <= 1}
                        onClick={() =>
                          setZonePages((current) => ({ ...current, [zone.id]: page - 1 }))
                        }
                        className="grid size-5 place-items-center rounded-md border border-border/60 bg-background transition-colors hover:bg-accent disabled:opacity-40"
                      >
                        ‹
                      </button>
                      <span className="whitespace-nowrap px-0.5">
                        {searching
                          ? t('playground.scope.matchCount').replace(
                              '{count}',
                              String(matchedTypes.length),
                            )
                          : t('playground.scope.pageLabel')
                              .replace('{page}', String(page))
                              .replace('{total}', String(totalPages))
                              .replace('{count}', String(types.length))}
                      </span>
                      <button
                        type="button"
                        aria-label={t('playground.scope.nextPage')}
                        data-testid={`playground-scope-zone-${zone.id}-page-next`}
                        disabled={searching || page >= totalPages}
                        onClick={() =>
                          setZonePages((current) => ({ ...current, [zone.id]: page + 1 }))
                        }
                        className="grid size-5 place-items-center rounded-md border border-border/60 bg-background transition-colors hover:bg-accent disabled:opacity-40"
                      >
                        ›
                      </button>
                    </span>
                  )}
                </div>
              </section>
            );
          })}
        </div>

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-border/70 pt-3">
          <p
            className="shrink-0 text-xs font-semibold text-foreground"
            data-testid="playground-scope-selected-count"
          >
            {t('playground.scope.selectedCount').replace('{count}', String(selectedTotal))}
          </p>
          <p
            className="min-w-0 flex-1 text-right text-xs leading-4 text-muted-foreground"
            data-testid="playground-scope-footer-note"
          >
            {t('playground.scope.footerNote')}
          </p>
        </div>
      </DialogContent>
    </Dialog>
  );
};
