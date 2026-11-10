// Copyright 2026 LexMask Contributors

import { useEffect, useRef, useState } from 'react';
import type { FC } from 'react';
import { ChevronDown } from 'lucide-react';
import { resolveLegalTextSelection, sameIdSelection } from '@/config/legalPreset';
import type { EntityTypeConfig } from '@/config/entityTypes';
import { Card } from '@/components/ui/card';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { useServiceHealth, type ServicesHealth } from '@/hooks/use-service-health';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { buildDefaultTextTypeIds } from '@/services/defaultRedactionPreset';
import type { usePlayground } from '../hooks/use-playground';
import { PlaygroundUploadDropzone } from './playground-upload-dropzone';
import { TextTypeGroups, VisionPipelines } from './playground-upload-config';
import { PresetSelectors, PresetSaveDialog } from './playground-upload-presets';

interface PlaygroundUploadProps {
  ctx: ReturnType<typeof usePlayground>;
}

type Service = NonNullable<ServicesHealth['services'][keyof ServicesHealth['services']]>;
type ServiceStatus = Service['status'];
type Translate = (key: string) => string;

const blockedServiceStatuses = new Set<ServiceStatus>(['offline', 'degraded']);

// 「默认清单」哨兵，与 playground-upload-presets.tsx 的 PresetRow 下拉同源；
// hook 里 playgroundPresetTextId 的运行时形态是 null（= 未追踪任何自定义清单），
// 只有哨兵语义、没有字面量 '__default__' 值。
const DEFAULT_PRESET_SENTINEL = '__default__';

function serviceStatusLabel(status: ServiceStatus, t: Translate) {
  return t(`health.${status}`);
}

type ServiceKey = 'paddle_ocr' | 'has_ner' | 'visual_features';

const SERVICE_KEYS: readonly ServiceKey[] = ['paddle_ocr', 'has_ner', 'visual_features'];

interface ServiceEntry {
  key: ServiceKey;
  service: Service;
}

function serviceSummary(entries: ServiceEntry[], t: Translate) {
  return entries
    .map((entry) => `${t(`health.service.${entry.key}`)}: ${serviceStatusLabel(entry.service.status, t)}`)
    .join(', ');
}

export const PlaygroundUpload: FC<PlaygroundUploadProps> = ({ ctx }) => {
  const t = useT();
  const { health, checking } = useServiceHealth();
  const { dropzone, uploadIssue, recognition: rec } = ctx;
  // Issue#262：高级折叠抽屉（默认收起）——文本/图像双 Tab、分类勾选与清单下拉都收进抽屉，
  // 识别前唯一显式类型选择是上方「法律卷宗（默认）/ 全部类型」chip 行。
  const [advancedTypesOpen, setAdvancedTypesOpen] = useState(false);
  const visualTypeCount =
    rec.pipelines.find((pipeline) => pipeline.mode === 'visual_features')?.types.length ?? 0;
  const selectedVisualTypeCount = rec.selectedVisualFeatureTypes.length;
  const services: ServiceEntry[] = health
    ? SERVICE_KEYS.flatMap((key) => {
        const service = health.services[key];
        return service ? [{ key, service }] : [];
      })
    : [];
  const blockedServices = services.filter((entry) => blockedServiceStatuses.has(entry.service.status));
  const backendChecking = !health && checking;
  const backendUnavailable = !health && !checking;
  const modelServicesBlocked = blockedServices.length > 0;
  const serviceHintKey = backendUnavailable
    ? 'playground.upload.offlineHint'
    : backendChecking
      ? 'playground.upload.checkingHint'
      : 'playground.upload.modelOfflineHint';
  const serviceActionKey = backendUnavailable
    ? 'playground.upload.offlineAction'
    : backendChecking
      ? 'playground.upload.checkingAction'
      : 'playground.upload.modelOfflineAction';
  const attentionSummary = serviceSummary(blockedServices, t);
  const uploadBlocked = backendUnavailable;
  const disabledReason = uploadBlocked
    ? attentionSummary
      ? `${t(serviceHintKey)} ${attentionSummary}`
      : t(serviceHintKey)
    : undefined;

  // 默认选中 effect 与 chip 点击用到的字段先解构（exhaustive-deps 按标识符收集依赖）。
  const {
    entityTypes,
    selectedTypes,
    playgroundPresetTextId,
    setSelectedTypes,
    clearPlaygroundTextPresetTracking,
  } = rec;
  const availableTextIds = entityTypes.map((type) => type.id);
  const legalSelection = resolveLegalTextSelection(availableTextIds);
  const catalogReady = availableTextIds.length > 0;
  const isLegalActive = catalogReady && sameIdSelection(selectedTypes, legalSelection);
  const isAllActive = catalogReady && sameIdSelection(selectedTypes, availableTextIds);

  // 法律卷宗 chip：只写 selectedTypes（请求层 submitRecognitionTask(fileId, selectedTypes, ...)
  // 透传契约不动）；视觉侧沿用后端默认 pipeline 集（official_seal / signature 本就在
  // default_enabled 集，见 config/legalPreset.ts 顶部说明）。
  const selectLegalPreset = () => {
    setSelectedTypes(resolveLegalTextSelection(entityTypes.map((type) => type.id)));
    clearPlaygroundTextPresetTracking();
  };
  const selectAllTypes = () => {
    setSelectedTypes(entityTypes.map((type) => type.id));
    clearPlaygroundTextPresetTracking();
  };

  // Issue#262 默认选中：挂载 effect——仅当清单追踪处于「默认清单」哨兵
  // （playgroundPresetTextId 命中 '__default__' 语义，运行时形态 null）且当前选择恰为
  // 后端默认集时，应用法律卷宗集。用户已选/保存过自定义清单（playgroundPresetTextId
  // 为 preset id）则不打扰。ref 以 entityTypes 引用为键、同一目录只应用一次；
  // 目录数组被重取替换时允许重新武装一次——识别 hook 挂载即 loadRecognitionConfig(false)
  // 会把选择重置回后端默认集，暖缓存下法律默认若不随之重放会被这次初载覆盖丢失。
  const legalAppliedForRef = useRef<EntityTypeConfig[] | null>(null);
  useEffect(() => {
    if (entityTypes.length === 0) return;
    if ((playgroundPresetTextId ?? DEFAULT_PRESET_SENTINEL) !== DEFAULT_PRESET_SENTINEL) return;
    if (!sameIdSelection(selectedTypes, buildDefaultTextTypeIds(entityTypes))) return;
    if (legalAppliedForRef.current === entityTypes) return;
    legalAppliedForRef.current = entityTypes;
    setSelectedTypes(resolveLegalTextSelection(entityTypes.map((type) => type.id)));
    clearPlaygroundTextPresetTracking();
  }, [
    entityTypes,
    selectedTypes,
    playgroundPresetTextId,
    setSelectedTypes,
    clearPlaygroundTextPresetTracking,
  ]);

  return (
    <div
      className="grid h-full min-h-0 min-w-0 flex-1 grid-cols-1 gap-3 overflow-hidden lg:grid-cols-[minmax(0,1fr)_minmax(28rem,30rem)] lg:items-stretch xl:grid-cols-[minmax(0,1fr)_minmax(30rem,32rem)] 2xl:grid-cols-[minmax(0,1fr)_minmax(32rem,34rem)]"
      data-testid="playground-upload"
    >
      <PlaygroundUploadDropzone
        dropzone={dropzone}
        disabled={uploadBlocked}
        disabledReason={disabledReason}
        uploadIssue={uploadIssue}
      />

      <Card
        className="page-surface min-h-0 w-full shrink-0 overflow-hidden border-border/70 bg-card lg:h-full lg:max-h-full lg:self-stretch"
        data-testid="playground-type-panel"
      >
        <div className="shrink-0 space-y-2.5 border-b border-border/70 px-3 py-2.5">
          <div className="min-w-0">
            <h3 className="text-sm font-semibold tracking-tight text-foreground">
              {t('playground.recognitionTypes')}
            </h3>
            <p
              className="mt-0.5 truncate text-xs leading-4 text-muted-foreground"
              title={t('playground.upload.configDesc')}
            >
              {t('playground.upload.configDesc')}
            </p>
          </div>
          {/* 1) chip 行：法律卷宗（默认）/ 全部类型 */}
          <div
            className="flex flex-wrap items-center gap-1.5"
            role="group"
            aria-label={t('playground.presetChipGroup')}
            data-testid="playground-preset-chip-row"
          >
            <button
              type="button"
              data-testid="playground-preset-chip-legal"
              aria-pressed={isLegalActive}
              onClick={selectLegalPreset}
              className={cn(
                'rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                isLegalActive
                  ? 'border-foreground bg-background text-foreground shadow-[var(--shadow-sm)]'
                  : 'border-border/70 bg-background/60 text-muted-foreground hover:border-border hover:text-foreground',
              )}
            >
              {t('playground.presetLegal')}
            </button>
            <button
              type="button"
              data-testid="playground-preset-chip-all"
              aria-pressed={isAllActive}
              onClick={selectAllTypes}
              className={cn(
                'rounded-full border px-3 py-1 text-xs font-medium transition-colors',
                isAllActive
                  ? 'border-foreground bg-background text-foreground shadow-[var(--shadow-sm)]'
                  : 'border-border/70 bg-background/60 text-muted-foreground hover:border-border hover:text-foreground',
              )}
            >
              {t('playground.presetAll')}
            </button>
          </div>
          {/* 2) 高级折叠抽屉开关（默认收起） */}
          <button
            type="button"
            data-testid="playground-advanced-types-toggle"
            aria-expanded={advancedTypesOpen}
            onClick={() => setAdvancedTypesOpen((open) => !open)}
            className="flex w-full items-center justify-between gap-2 rounded-xl border border-border/70 bg-muted/35 px-3 py-1.5 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent/35 hover:text-foreground"
          >
            {t('playground.advancedTypes')}
            <ChevronDown
              className={cn('size-3.5 shrink-0 transition-transform', advancedTypesOpen && 'rotate-180')}
            />
          </button>
          {(backendUnavailable ||
            backendChecking ||
            modelServicesBlocked) && (
            <div
              className="max-h-[4.75rem] space-y-1 overflow-hidden rounded-xl border border-[var(--warning-border)] bg-[var(--warning-surface)] px-2.5 py-1.5 text-[11px] leading-4 text-[var(--warning-foreground)]"
              data-testid="playground-offline-hint"
            >
              <p className="truncate" title={t(serviceHintKey)}>
                {t(serviceHintKey)}
              </p>
              {attentionSummary && (
                <p className="truncate" title={attentionSummary}>
                  <span className="font-medium">{t('playground.upload.affectedServices')}</span>{' '}
                  <span data-testid="playground-affected-services">{attentionSummary}</span>
                </p>
              )}
              <p className="truncate" title={t(serviceActionKey)}>
                <span className="font-medium">{t('playground.upload.nextAction')}</span>{' '}
                {t(serviceActionKey)}
              </p>
            </div>
          )}
        </div>

        {/* 3) 高级折叠抽屉（默认收起）：原 text/vision 双 Tab + 分类勾选 + 清单下拉 + 保存清单弹窗原样搬入 */}
        {advancedTypesOpen && (
          <Tabs
            value={rec.typeTab}
            onValueChange={(value) => rec.setTypeTab(value as 'text' | 'vision')}
            className="flex min-h-0 flex-1 flex-col"
          >
            <div className="shrink-0 space-y-2.5 border-b border-border/70 px-3 py-2.5">
              <div className="flex items-center justify-end gap-2.5">
                <TabsList className="h-8 shrink-0 rounded-full border border-border/70 bg-muted/35 p-1">
                  <TabsTrigger
                    value="text"
                    className="whitespace-nowrap rounded-full px-3 py-1 text-xs"
                    data-testid="playground-tab-text"
                  >
                    {t('playground.text')}
                    <span className="ml-1 tabular-nums text-[10px] text-muted-foreground">
                      {rec.entityTypes.length}
                    </span>
                  </TabsTrigger>
                  <TabsTrigger
                    value="vision"
                    className="whitespace-nowrap rounded-full px-3 py-1 text-xs"
                    data-testid="playground-tab-vision"
                  >
                    {t('playground.vision')}
                    <span className="ml-1 tabular-nums text-[10px] text-muted-foreground">
                      {rec.visionTypes.length}
                    </span>
                  </TabsTrigger>
                </TabsList>
              </div>
              <PresetSelectors
                rec={rec}
                disabledText={rec.textConfigState !== 'ready'}
                disabledVision={rec.visionConfigState !== 'ready'}
              />
            </div>

            <div className="page-surface-body flex min-h-0 flex-1 flex-col overflow-hidden">
              <TabsContent
                value="text"
                className="mt-0 h-full min-h-0 flex-col gap-2 p-2 pb-0 data-[state=active]:flex"
              >
                <div className="flex shrink-0 items-center justify-between gap-3 rounded-[20px] border border-border/70 bg-muted/20 px-3 py-2">
                  <p className="text-sm font-semibold tracking-[-0.02em] text-foreground">
                    {t('playground.text')}
                  </p>
                  <p className="shrink-0 whitespace-nowrap text-xs leading-4 text-muted-foreground">
                    {rec.selectedTypes.length} / {rec.entityTypes.length} {t('playground.selected')}
                  </p>
                </div>
                <TextTypeGroups rec={rec} />
              </TabsContent>
              <TabsContent
                value="vision"
                className="mt-0 h-full min-h-0 flex-col gap-2 p-2 pb-0 data-[state=active]:flex"
              >
                <div className="flex shrink-0 items-center justify-between gap-3 rounded-[20px] border border-border/70 bg-muted/20 px-3 py-2">
                  <p className="text-sm font-semibold tracking-[-0.02em] text-foreground">
                    {t('playground.vision')}
                  </p>
                  <p className="min-w-0 shrink truncate text-right text-xs leading-4 text-muted-foreground">
                    {t('playground.ocrShort')} {rec.selectedOcrHasTypes.length} /{' '}
                    {rec.pipelines.find((pipeline) => pipeline.mode === 'ocr_has')?.types.length ?? 0}
                    <span className="mx-2 text-border">|</span>
                    {t('playground.visualFeatureShort')} {selectedVisualTypeCount} / {visualTypeCount}
                  </p>
                </div>
                <VisionPipelines rec={rec} />
              </TabsContent>
            </div>

          </Tabs>
        )}
      </Card>
      <PresetSaveDialog rec={rec} />
    </div>
  );
};
