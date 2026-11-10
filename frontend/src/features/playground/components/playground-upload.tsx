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
import { resolveTextTypeName, TextTypeGroups, VisionPipelines } from './playground-upload-config';
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
  // Issue#262 Task 9b：告警「详情」折叠——黑话细节（启动命令等）默认不铺开。
  const [offlineDetailsOpen, setOfflineDetailsOpen] = useState(false);
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
  // Issue#262 Task 9b 告警人话化：视觉特征服务离线走专属人话文案，且仅当当前选中
  // 含视觉要素时才显示（印章/签名在 visual_features 管线，服务键 visual_features）；
  // 视觉离线但未选视觉要素 → 不打扰（侧边栏服务状态与抽屉内 VisualServiceBadge 仍有呈现）。
  // 其余服务（paddle_ocr/has_ner）离线仍走通用「识别模型未全部启动」提示。
  const visualOffline = health?.services.visual_features?.status === 'offline';
  const showVisualWarning = visualOffline && selectedVisualTypeCount > 0;
  const otherServicesBlocked =
    blockedServices.filter((entry) => entry.key !== 'visual_features').length > 0;
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
  const showOfflineHint =
    backendUnavailable || backendChecking || showVisualWarning || otherServicesBlocked;
  const warningMainText = showVisualWarning
    ? t('playground.upload.visualOfflineHint').replace('{count}', String(selectedVisualTypeCount))
    : t(serviceHintKey);

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
  // Task 9b 标签墙：当前 selectedTypes 的实时镜像（目录序；抽屉勾选与 chip 切换同步）。
  const selectedTagTypes = entityTypes.filter((type) => selectedTypes.includes(type.id));

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
    // Issue#262 Task 9b：纵向布局（界面稿形态）= 上传大卡 → 摘要卡。
    // 根容器不设 min-h-0：flex min-height:auto 以内容为下限——抽屉展开时内容自然撑高、
    // 外层 page-stack 滚动；收起时 flex-1 吃掉剩余高度，页面不留大片空白。
    <div className="flex min-w-0 flex-1 flex-col gap-3.5" data-testid="playground-upload">
      <PlaygroundUploadDropzone
        dropzone={dropzone}
        disabled={uploadBlocked}
        disabledReason={disabledReason}
        uploadIssue={uploadIssue}
      />

      {/* 摘要卡（容器 testid 沿用 playground-type-panel）：chip 行 + 标签墙 + 说明行 + 人话告警，
          「高级 · 按类型微调」入口本任务仍开 T6 抽屉（Task 10b 换弹窗）。 */}
      <Card
        className="w-full shrink-0 overflow-hidden border-border/70 bg-card"
        data-testid="playground-type-panel"
      >
        <div className="flex flex-col gap-2.5 px-3.5 py-3">
          <h3 className="sr-only">{t('playground.recognitionTypes')}</h3>
          {/* 1) chip 行（法律卷宗（默认）/ 全部类型）+ 高级入口 */}
          <div className="flex flex-wrap items-center gap-2">
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
            <button
              type="button"
              data-testid="playground-advanced-types-toggle"
              aria-expanded={advancedTypesOpen}
              onClick={() => setAdvancedTypesOpen((open) => !open)}
              className="ml-auto flex shrink-0 items-center gap-1 rounded-full border border-border/70 bg-muted/35 px-3 py-1 text-xs font-medium text-muted-foreground transition-colors hover:bg-accent/35 hover:text-foreground"
            >
              {t('playground.advancedTypes')}
              <ChevronDown
                className={cn('size-3.5 shrink-0 transition-transform', advancedTypesOpen && 'rotate-180')}
              />
            </button>
          </div>
          {/* 2) 标签墙：selectedTypes 实时镜像（抽屉勾选同步） */}
          {selectedTagTypes.length > 0 && (
            <div className="flex flex-wrap gap-1.5" data-testid="playground-selected-tags">
              {selectedTagTypes.map((type) => (
                <span
                  key={type.id}
                  data-testid={`playground-selected-tag-${type.id}`}
                  className="rounded-md border border-border/60 bg-muted/30 px-1.5 py-0.5 text-[11px] leading-4 text-foreground/85"
                  title={resolveTextTypeName(type.id, type.name, t)}
                >
                  {resolveTextTypeName(type.id, type.name, t)}
                </span>
              ))}
            </div>
          )}
          {/* 3) 说明行（预览口径） */}
          <p className="text-xs leading-4 text-muted-foreground">
            {t('playground.upload.summaryNote')}
          </p>
          {/* 4) 告警（人话主文案 + 「详情」折叠收黑话细节） */}
          {showOfflineHint && (
            <div
              className="rounded-xl border border-[var(--warning-border)] bg-[var(--warning-surface)] px-2.5 py-1.5 text-xs leading-4 text-[var(--warning-foreground)]"
              data-testid="playground-offline-hint"
            >
              <div className="flex items-start justify-between gap-2">
                <p className="min-w-0 flex-1">{warningMainText}</p>
                <button
                  type="button"
                  data-testid="playground-offline-hint-details"
                  aria-expanded={offlineDetailsOpen}
                  onClick={() => setOfflineDetailsOpen((open) => !open)}
                  className="flex shrink-0 items-center gap-0.5 rounded-full px-1.5 py-0.5 text-[11px] font-medium transition-colors hover:bg-[var(--warning-foreground)]/10"
                >
                  {t('playground.upload.detailsToggle')}
                  <ChevronDown
                    className={cn(
                      'size-3 shrink-0 transition-transform',
                      offlineDetailsOpen && 'rotate-180',
                    )}
                  />
                </button>
              </div>
              {offlineDetailsOpen && (
                <div className="mt-1 space-y-0.5 border-t border-[var(--warning-foreground)]/25 pt-1">
                  {attentionSummary && (
                    <p className="break-words">
                      <span className="font-medium">{t('playground.upload.affectedServices')}</span>{' '}
                      <span data-testid="playground-affected-services">{attentionSummary}</span>
                    </p>
                  )}
                  <p className="break-words">
                    <span className="font-medium">{t('playground.upload.nextAction')}</span>{' '}
                    {t(serviceActionKey)}
                  </p>
                </div>
              )}
            </div>
          )}
          {/* 5) 高级折叠抽屉（默认收起）：原 text/vision 双 Tab + 分类勾选 + 清单下拉 + 保存清单弹窗原样搬入 */}
          {advancedTypesOpen && (
            <Tabs
              value={rec.typeTab}
              onValueChange={(value) => rec.setTypeTab(value as 'text' | 'vision')}
              className="flex min-h-0 flex-col"
            >
              <div className="shrink-0 space-y-2.5 border-b border-border/70 pb-2.5">
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

              <div className="flex min-h-0 flex-col">
                <TabsContent
                  value="text"
                  className="mt-0 min-h-0 flex-col gap-2 p-2 pb-0 data-[state=active]:flex"
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
                  className="mt-0 min-h-0 flex-col gap-2 p-2 pb-0 data-[state=active]:flex"
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
        </div>
      </Card>
      <PresetSaveDialog rec={rec} />
    </div>
  );
};
