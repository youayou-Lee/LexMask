// Copyright 2026 LexMask Contributors

import { useEffect, useRef, useState } from 'react';
import type { FC } from 'react';
import { Check, ChevronDown, ChevronRight, FileText, SlidersHorizontal } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { resolveLegalTextSelection, sameIdSelection } from '@/config/legalPreset';
import type { EntityTypeConfig } from '@/config/entityTypes';
import { Card } from '@/components/ui/card';
import { useServiceHealth, type ServicesHealth } from '@/hooks/use-service-health';
import { useI18n, useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { fileApi } from '@/services/api';
import { buildDefaultTextTypeIds } from '@/services/defaultRedactionPreset';
import { useUiTheme } from '@/theme/ui-theme';
import type { FileListItem } from '@/types';
import type { usePlayground } from '../hooks/use-playground';
import { PlaygroundUploadDropzone } from './playground-upload-dropzone';
import { resolveTextTypeName } from './playground-upload-config';
import { RecognitionScopeDialog } from './recognition-scope-dialog';

interface PlaygroundUploadProps {
  ctx: ReturnType<typeof usePlayground>;
}

type Service = NonNullable<ServicesHealth['services'][keyof ServicesHealth['services']]>;
type ServiceStatus = Service['status'];
type Translate = (key: string) => string;

const blockedServiceStatuses = new Set<ServiceStatus>(['offline', 'degraded']);

// 「默认清单」哨兵：hook 里 playgroundPresetTextId 的运行时形态是 null
// （= 未追踪任何自定义清单），只有哨兵语义、没有字面量 '__default__' 值。
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

// Issue#355 Agent C（ds 投料态）·最近处理：playground 源最近文件（只读 GET /files），
// 行为=既有 ?file_id= 深链恢复协议（playground-page resume effect 承接）；拉取失败/为空
// 整块不渲染（宁缺勿谎）。业务流程零改动：不新增端点、不改路由。
const RECENT_FILE_LIMIT = 4;

function formatRecentTime(value: string | null | undefined, locale: string): string {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '';
  const isEn = locale === 'en';
  const now = new Date();
  const startOfDay = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const dayDiff = Math.round((startOfDay(now) - startOfDay(date)) / 86_400_000);
  const hm = date.toLocaleTimeString(isEn ? 'en-US' : 'zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  });
  if (dayDiff === 0) return isEn ? `Today ${hm}` : `今天 ${hm}`;
  if (dayDiff === 1) return isEn ? `Yesterday ${hm}` : `昨天 ${hm}`;
  const md = `${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  return `${md} ${hm}`;
}

const DsRecentFiles: FC = () => {
  const t = useT();
  const locale = useI18n((state) => state.locale);
  const [, setSearchParams] = useSearchParams();
  const [rows, setRows] = useState<FileListItem[] | null>(null);

  useEffect(() => {
    let cancelled = false;
    fileApi
      .list(1, RECENT_FILE_LIMIT, { source: 'playground' })
      .then((res) => {
        if (!cancelled) setRows(Array.isArray(res?.files) ? res.files : []);
      })
      .catch(() => {
        if (!cancelled) setRows([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (!rows || rows.length === 0) return null;

  return (
    <div className="ds-recent" data-testid="playground-recent">
      <div className="ds-recent-title">{t('playground.upload.recentTitle')}</div>
      {rows.map((row) => {
        const done = Boolean(row.has_output);
        const time = formatRecentTime(row.created_at, locale);
        return (
          <button
            key={row.file_id}
            type="button"
            className="ds-recent-row"
            data-testid={`playground-recent-row-${row.file_id}`}
            onClick={() => setSearchParams(new URLSearchParams({ file_id: row.file_id }))}
          >
            <span className="ds-recent-ic" aria-hidden>
              <FileText />
            </span>
            <span className="min-w-0 flex-1">
              <span className="ds-recent-name block">{row.original_filename}</span>
              {time && <span className="ds-recent-meta block">{time}</span>}
            </span>
            <span className="ds-recent-st">
              {done ? t('playground.upload.recentResult') : t('playground.upload.recentResume')}
            </span>
          </button>
        );
      })}
    </div>
  );
};

export const PlaygroundUpload: FC<PlaygroundUploadProps> = ({ ctx }) => {
  const t = useT();
  // Issue#355 Agent C：ds=产品哲学样板（大上传页+小配置条，无配置面板/开关墙）；
  // 识别项三区弹窗入口平移进配置条「高级设置」。classic 分支 DOM 零漂移。
  const dsMode = useUiTheme((s) => s.theme) === 'ds';
  const { health, checking } = useServiceHealth();
  const { dropzone, uploadIssue, recognition: rec } = ctx;
  // Issue#262 二期 Task 10b：「高级 · 按类型微调」改开识别范围三区弹窗（替换 T6 内联抽屉）。
  // 弹窗按需挂载（关闭不渲染）——Radix portal + usePresets 等 hook 只在打开时运行。
  const [scopeDialogOpen, setScopeDialogOpen] = useState(false);
  // Issue#262 Task 9b：告警「详情」折叠——黑话细节（启动命令等）默认不铺开。
  const [offlineDetailsOpen, setOfflineDetailsOpen] = useState(false);
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
  // 视觉离线但未选视觉要素 → 不打扰（侧边栏服务状态与弹窗内服务徽标仍有呈现）。
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
  // Issue#270：组合故障（视觉 + 其他服务同时 blocked）主文案换组合版键——单视觉文案
  // 结尾「其余识别不受影响」在组合故障下是错误陈述。分支序：组合 > 单视觉 > 通用。
  const warningMainText =
    showVisualWarning && otherServicesBlocked
      ? t('playground.upload.visualOfflineCombinedHint').replace(
          '{count}',
          String(selectedVisualTypeCount),
        )
      : showVisualWarning
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
  // Issue#267：目录就绪由 textConfigState 状态机派生（'ready' 仅在目录非空时设置，
  // use-playground-recognition.ts），不再依赖「数组非空」的间接推断；两处实现漂移时以状态机为准。
  const catalogReady = rec.textConfigState === 'ready';
  const isLegalActive = catalogReady && sameIdSelection(selectedTypes, legalSelection);
  const isAllActive = catalogReady && sameIdSelection(selectedTypes, availableTextIds);
  // Issue#267：目录未就绪时 chip 置灰 + title 把「为什么置灰」讲清楚（三态各有文案）。
  const catalogHintKey =
    !catalogReady
      ? rec.textConfigState === 'loading'
        ? 'playground.scope.catalogLoading'
        : rec.textConfigState === 'empty'
          ? 'playground.scope.catalogEmpty'
          : 'playground.scope.catalogUnavailable'
      : undefined;
  // Task 9b 标签墙：当前 selectedTypes 的实时镜像（目录序；弹窗勾选与 chip 切换同步）。
  const selectedTagTypes = entityTypes.filter((type) => selectedTypes.includes(type.id));
  // Task 10b：OCR/LA 两区是否适用 = 扫描件/图片（与 use-playground-file.ts 的
  // isVisionFile 判定同口径）。上传态通常还没有文件记录（fileInfo=null）→ 按
  // 「扫描件预览」口径三区全可用；一旦出现纯文本文件记录，两区在弹窗里收成不适用。
  const visionApplicable = ctx.fileInfo
    ? Boolean(ctx.fileInfo.is_scanned) || ctx.fileInfo.file_type === 'image'
    : true;

  // 法律卷宗 chip：只写 selectedTypes（请求层 submitRecognitionTask(fileId, selectedTypes, ...)
  // 透传契约不动）；视觉侧沿用后端默认 pipeline 集（official_seal / signature 本就在
  // default_enabled 集，见 config/legalPreset.ts 顶部说明）。
  const selectLegalPreset = () => {
    // Issue#267：防御守卫——目录数组为空时 resolveLegalTextSelection 会回落空集
    // （legalPreset.ts fail-open 分支），宁可不动也不写空集。覆盖 ready∧空 的人工态
    // 与任何状态机/数组漂移；常态路径由 chip disabled 拦截。
    if (entityTypes.length === 0) return;
    setSelectedTypes(resolveLegalTextSelection(entityTypes.map((type) => type.id)));
    clearPlaygroundTextPresetTracking();
  };
  const selectAllTypes = () => {
    if (entityTypes.length === 0) return;
    setSelectedTypes(entityTypes.map((type) => type.id));
    clearPlaygroundTextPresetTracking();
  };

  // Issue#262 默认选中：挂载 effect——仅当清单追踪处于「默认清单」哨兵
  // （playgroundPresetTextId 命中 '__default__' 语义，运行时 null）且当前选择恰为
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

  // 告警块（classic/ds 共用同一 JSX，双轨同源防漂移）
  const offlineHintBlock = showOfflineHint ? (
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
          aria-controls="playground-offline-hint-details-panel"
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
      {/* Issue#266：aria-controls 引用条件挂载的 id——收起时不在 DOM，ARIA
          disclosure 允许指向暂缺元素；不改永久挂载+hidden（渲染行为变更出界）。 */}
      {offlineDetailsOpen && (
        <div
          id="playground-offline-hint-details-panel"
          className="mt-1 space-y-0.5 border-t border-[var(--warning-foreground)]/25 pt-1"
        >
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
  ) : null;

  // ds 投料态「高级」入口：与 classic 的 playground-scope-entry 同一 testid/ARIA/弹窗，
  // 仅落位与文案不同（真稿 .cfg-adv「高级设置 ▸」）。
  const scopeEntryButton = (
    <button
      type="button"
      data-testid="playground-scope-entry"
      aria-haspopup="dialog"
      aria-expanded={scopeDialogOpen}
      onClick={() => setScopeDialogOpen(true)}
      className={cn(
        'shrink-0 items-center gap-1 text-xs font-medium transition-colors',
        dsMode
          ? 'ds-cfg-adv'
          : 'ml-auto flex rounded-full border border-border/70 bg-muted/35 px-3 py-1 text-muted-foreground hover:bg-accent/35 hover:text-foreground',
      )}
    >
      {dsMode ? (
        <>
          {t('playground.upload.advancedSettings')}
          <ChevronRight className="size-3 shrink-0" />
        </>
      ) : (
        <>
          <SlidersHorizontal className="size-3.5 shrink-0" />
          {t('playground.advancedTypes')}
        </>
      )}
    </button>
  );

  if (!dsMode) {
    return (
      // Issue#262 Task 9b：纵向布局（界面稿形态）= 上传大卡 → 摘要卡。
      // 根容器不设 min-h-0：flex min-height:auto 以内容为下限——内容自然撑高、
      // 外层 page-stack 滚动；收起时 flex-1 吃掉剩余高度，页面不留大片空白。
      <div className="flex min-w-0 flex-1 flex-col gap-3.5" data-testid="playground-upload">
        <PlaygroundUploadDropzone
          dropzone={dropzone}
          disabled={uploadBlocked}
          disabledReason={disabledReason}
          uploadIssue={uploadIssue}
        />

        {/* 摘要卡（容器 testid 沿用 playground-type-panel）：chip 行 + 标签墙 + 说明行 + 人话告警，
            「高级 · 按类型微调」入口打开识别范围三区弹窗（Task 10b）。 */}
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
                  disabled={!catalogReady}
                  title={catalogHintKey ? t(catalogHintKey) : undefined}
                  onClick={selectLegalPreset}
                  className={cn(
                    'rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
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
                  disabled={!catalogReady}
                  title={catalogHintKey ? t(catalogHintKey) : undefined}
                  onClick={selectAllTypes}
                  className={cn(
                    'rounded-full border px-3 py-1 text-xs font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
                    isAllActive
                      ? 'border-foreground bg-background text-foreground shadow-[var(--shadow-sm)]'
                      : 'border-border/70 bg-background/60 text-muted-foreground hover:border-border hover:text-foreground',
                  )}
                >
                  {t('playground.presetAll')}
                </button>
              </div>
              {scopeEntryButton}
            </div>
            {/* 2) 标签墙：selectedTypes 实时镜像（弹窗勾选同步） */}
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
            {offlineHintBlock}
          </div>
        </Card>
        {/* 识别范围三区弹窗：按需挂载（关闭即卸载，usePresets 等只在打开时运行） */}
        {scopeDialogOpen && (
          <RecognitionScopeDialog
            open
            onOpenChange={setScopeDialogOpen}
            rec={rec}
            visionApplicable={visionApplicable}
          />
        )}
      </div>
    );
  }

  // ── ds 投料态（产品哲学样板，真稿 ios-single-upload）──────────────────
  // 大拖拽位 + 小配置条（方案 chip + ✓保留水印 + ✓页码过滤 + 高级▸）+ 最近处理。
  // 产品铁律：投料态零配置面板/零开关墙——preset chips/标签墙不渲染（识别项
  // 细分全部收进「高级设置」三区弹窗），默认值替选择。
  return (
    <div className="flex min-w-0 flex-1 flex-col" data-testid="playground-upload">
      <div className="ds-upload-hero flex flex-col" data-testid="playground-upload-hero">
        <PlaygroundUploadDropzone
          dropzone={dropzone}
          disabled={uploadBlocked}
          disabledReason={disabledReason}
          uploadIssue={uploadIssue}
        />

        {offlineHintBlock}

        {/* 小配置条：默认值替选择（真稿 .config-strip） */}
        <div className="ds-cfg-strip" data-testid="playground-config-strip">
          <span className="ds-cfg-label">{t('playground.upload.schemeLabel')}</span>
          <span className="ds-cfg-chip" data-testid="playground-scheme-chip">
            {t('playground.upload.schemeDefault').replace(
              '{count}',
              String(selectedTypes.length),
            )}
          </span>
          {rec.watermarkDelivery === 'keep' && (
            <span className="ds-cfg-chip" data-testid="playground-cfg-watermark">
              <Check aria-hidden />
              {t('playground.upload.watermarkKeep')}
            </span>
          )}
          <span className="ds-cfg-chip" data-testid="playground-pagefilter-chip">
            <Check aria-hidden />
            {t('playground.upload.pageFilter')}
          </span>
          {scopeEntryButton}
        </div>

        {/* 最近处理：playground 源最近文件，?file_id= 深链恢复（既有协议） */}
        <DsRecentFiles />
      </div>

      {/* 识别范围三区弹窗：按需挂载（关闭即卸载，usePresets 等只在打开时运行） */}
      {scopeDialogOpen && (
        <RecognitionScopeDialog
          open
          onOpenChange={setScopeDialogOpen}
          rec={rec}
          visionApplicable={visionApplicable}
        />
      )}
    </div>
  );
};
