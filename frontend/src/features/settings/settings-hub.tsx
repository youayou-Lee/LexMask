// Copyright 2026 LexMask Contributors

import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useT } from '@/i18n';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { InteractionLockOverlay } from '@/components/InteractionLockOverlay';
import { useEntityTypes } from './hooks/use-entity-types';
import { EntityTypeList } from './components/entity-type-list';
import { EntityTypeDialog } from './components/entity-type-dialog';
import { PipelineConfigPanel } from './components/pipeline-config';
import { RedactionList } from './redaction-list';
import { WordPoolsSettings } from './word-pools';

// Issue#262 D3：/settings 单入口三页签。`tab` 与 URL `?tab=` 双向同步，
// 缺省页签 types 不占 URL 参数（与 use-history-data 的「all → delete 参数」惯例一致）。
// Issue#267：types 目录数据（useEntityTypes 及其全部消费）整体下沉进
// EntityTypeSettingsPanel——hub 顶层不再无条件发起目录请求：
// ① 首帧 spinner 只占 types 面板区，页签栏常驻可达；
// ② ?tab=redaction / ?tab=words 深链首帧不被 types fetch 阻塞；
// ③ loadError Alert 跟着数据走（只在 types 页签渲染，redaction/words 各有自己的失败态）。
// Radix TabsContent inactive 卸载 = 每次激活 types 才挂载面板并请求；
// 再进入重取 + 面板内 spinner 是有意拍板（钉在 use-entity-types.roundtrip.test.tsx A7）。
type HubTab = 'types' | 'redaction' | 'words';

function readHubTab(raw: string | null): HubTab {
  return raw === 'redaction' || raw === 'words' ? raw : 'types';
}

export function SettingsHub() {
  const t = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const activeTab = readHubTab(searchParams.get('tab'));
  const handleTabChange = (next: string) => {
    const nextParams = new URLSearchParams(searchParams);
    if (next === 'types') nextParams.delete('tab');
    else nextParams.set('tab', next);
    setSearchParams(nextParams, { replace: true });
  };

  return (
    <div className="saas-page flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden bg-background">
      <div className="page-shell !max-w-[min(100%,1920px)] !px-3 !py-2 sm:!px-4 sm:!py-3">
        <Tabs
          value={activeTab}
          onValueChange={handleTabChange}
          className="page-stack gap-3 overflow-hidden"
        >
          <TabsList
            className="shrink-0 self-start rounded-xl border border-border/70 bg-muted/40 p-1"
            data-testid="settings-hub-tabs"
          >
            <TabsTrigger
              value="types"
              className="whitespace-nowrap"
              data-testid="settings-tab-types"
            >
              {t('settings.tab.types')}
            </TabsTrigger>
            <TabsTrigger
              value="redaction"
              className="whitespace-nowrap"
              data-testid="settings-tab-redaction"
            >
              {t('settings.tab.redaction')}
            </TabsTrigger>
            <TabsTrigger
              value="words"
              className="whitespace-nowrap"
              data-testid="settings-tab-words"
            >
              {t('settings.tab.words')}
            </TabsTrigger>
          </TabsList>

          <TabsContent
            value="types"
            className="mt-0 flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-hidden"
          >
            <EntityTypeSettingsPanel />
          </TabsContent>

          <TabsContent
            value="redaction"
            className="mt-0 flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
          >
            {/* Issue#262 D3：RedactionList / WordPoolsSettings 是自包含整页组件（自带
                saas-kicker 页头），嵌入页签后与外层页签行会形成双层头部——是否需要
                视觉收敛留待门④真机走查决定，本任务不做视觉重构。 */}
            <RedactionList />
          </TabsContent>

          <TabsContent
            value="words"
            className="mt-0 flex min-h-0 min-w-0 flex-1 flex-col overflow-hidden"
          >
            <WordPoolsSettings />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

// Issue#267：types 页签面板——原 hub 顶层内容（useEntityTypes 消费 + 内层 text/vision
// 页签 + 导入导出 + 弹窗族）整体下沉；组件树与行为不变，只换挂载位置。
function EntityTypeSettingsPanel() {
  const t = useT();
  const panelIntroClass =
    'surface-subtle flex min-h-10 items-center px-4 py-2.5 text-xs leading-5 text-muted-foreground';
  const {
    entityTypes: _entityTypes,
    textTaxonomy,
    pipelines,
    loading,
    pipelinesLoading,
    textRuleTypes,
    loadError,
    importFileRef,
    createType,
    updateType,
    updateTypeOverride,
    deleteType,
    resetToDefault,
    createPipelineType,
    updatePipelineType,
    deletePipelineType,
    resetPipelines,
    handleExportPresets,
    handleImportPresets,
    fetchEntityTypes,
    fetchPipelines,
  } = useEntityTypes();

  const [dialogOpen, setDialogOpen] = useState(false);
  const [confirmState, setConfirmState] = useState<{
    title: string;
    message: string;
    danger?: boolean;
    onConfirm: () => void | Promise<void>;
  } | null>(null);

  const [editingType, setEditingType] = useState<(typeof _entityTypes)[number] | null>(null);
  const [operationLoading, setOperationLoading] = useState(false);
  const [saving, setSaving] = useState(false);

  const runLocked = async (action: () => void | Promise<void>) => {
    if (operationLoading) return;
    setOperationLoading(true);
    try {
      await action();
    } finally {
      setOperationLoading(false);
    }
  };

  const openAdd = () => {
    setEditingType(null);
    setDialogOpen(true);
  };

  const openEdit = (tp: (typeof _entityTypes)[number]) => {
    setEditingType(tp);
    setDialogOpen(true);
  };

  const handleSave = async (form: {
    name: string;
    description: string;
    regex_pattern: string;
    use_llm: boolean;
    tag_template: string;
    data_domain: string;
    generic_target: string;
    coref_enabled: boolean;
  }) => {
    setSaving(true);
    try {
      if (editingType) {
        const ok = await updateType(editingType.id, form);
        if (ok) {
          setDialogOpen(false);
          setEditingType(null);
        }
        return;
      }
      const ok = await createType({
        name: form.name,
        description: form.description,
        regex_pattern: form.regex_pattern,
        use_llm: form.use_llm,
        tag_template: form.tag_template,
        data_domain: form.data_domain,
        generic_target: form.generic_target,
        coref_enabled: form.coref_enabled,
      });
      if (ok) setDialogOpen(false);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <div className="h-7 w-7 animate-spin rounded-full border-2 border-muted border-t-foreground" />
      </div>
    );
  }

  return (
    <>
      {/* Issue#262 D3：原 /settings 整页内容（文本/图像识别项两块 + 导出/导入按钮）
          原样内嵌为「识别项设置」页签；既有 testid（settings-tabs/tab-text/
          tab-vision/export-presets/import-presets）全部保留在内部。 */}
      <Tabs defaultValue="text" className="page-stack gap-3 overflow-hidden">
        {loadError && (
          <Alert variant="destructive" data-testid="settings-load-error">
            <AlertDescription className="flex items-center justify-between gap-3">
              <span>{loadError}</span>
              <Button
                size="sm"
                variant="outline"
                className="h-7 shrink-0"
                data-testid="settings-load-error-retry"
                onClick={() => {
                  void fetchEntityTypes();
                  void fetchPipelines();
                }}
              >
                {t('common.retry')}
              </Button>
            </AlertDescription>
          </Alert>
        )}

        <div className="flex shrink-0 flex-wrap items-center justify-between gap-3">
          <TabsList
            className="rounded-xl border border-border/70 bg-muted/40 p-1"
            data-testid="settings-tabs"
          >
            <TabsTrigger value="text" className="whitespace-nowrap" data-testid="tab-text">
              {t('settings.textRules')}
            </TabsTrigger>
            <TabsTrigger
              value="vision"
              className="whitespace-nowrap"
              data-testid="tab-vision"
            >
              {t('settings.visionRules')}
            </TabsTrigger>
          </TabsList>
          <div className="control-cluster">
            <Button
              size="sm"
              variant="outline"
              className="whitespace-nowrap"
              onClick={() => void runLocked(handleExportPresets)}
              disabled={operationLoading}
              data-testid="export-presets"
            >
              {t('settings.exportPresets')}
            </Button>
            <Button
              size="sm"
              variant="outline"
              className="whitespace-nowrap"
              onClick={() => importFileRef.current?.click()}
              disabled={operationLoading}
              data-testid="import-presets"
            >
              {t('settings.importPresets')}
            </Button>
            <input
              ref={importFileRef}
              type="file"
              accept=".json"
              onChange={(event) => void runLocked(() => handleImportPresets(event))}
              className="hidden"
            />
          </div>
        </div>

        <TabsContent
          value="text"
          className="mt-0 flex min-h-0 flex-1 flex-col gap-3 overflow-hidden"
        >
          <div className={panelIntroClass} data-testid="settings-text-intro">
            {t('settings.textPipelineIntro')}
          </div>
          <div className="flex min-h-0 flex-1 overflow-hidden">
            <EntityTypeList
              types={textRuleTypes}
              variant="llm"
              onAdd={openAdd}
              onEdit={openEdit}
              onOverrideChange={(type, patch) => {
                void runLocked(async () => {
                  await updateTypeOverride(type.id, patch);
                });
              }}
              onDelete={(id) =>
                setConfirmState({
                  title: t('common.delete'),
                  message: t('settings.confirmDeleteType'),
                  danger: true,
                  onConfirm: () => deleteType(id),
                })
              }
              onReset={() =>
                setConfirmState({
                  title: t('settings.resetTextRules'),
                  message: t('settings.confirmReset'),
                  danger: true,
                  onConfirm: () => resetToDefault(),
                })
              }
            />
          </div>
        </TabsContent>

        <TabsContent
          value="vision"
          className="mt-0 flex min-h-0 flex-1 flex-col gap-3 overflow-hidden"
        >
          <div className={panelIntroClass} data-testid="settings-vision-intro">
            {t('settings.pipelineDisplayName.ocr')} /{' '}
            {t('settings.pipelineDisplayName.image')}
          </div>
          <PipelineConfigPanel
            loading={pipelinesLoading}
            pipelines={pipelines}
            onCreateType={createPipelineType}
            onUpdateType={updatePipelineType}
            onDeleteType={(mode, id) =>
              setConfirmState({
                title: t('common.delete'),
                message: t('settings.confirmDeleteType'),
                danger: true,
                onConfirm: () => deletePipelineType(mode, id),
              })
            }
            onReset={() =>
              setConfirmState({
                title: t('settings.resetVisionRules'),
                message: t('settings.confirmResetPipelines'),
                danger: true,
                onConfirm: () => resetPipelines(),
              })
            }
          />
        </TabsContent>
      </Tabs>

      <EntityTypeDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        mode={editingType ? 'edit' : 'create'}
        saving={saving}
        initial={
          editingType
            ? {
                name: editingType.name,
                description: editingType.description ?? '',
                regex_pattern: editingType.regex_pattern ?? '',
                use_llm: !!editingType.use_llm,
                tag_template: editingType.tag_template ?? '',
                data_domain: editingType.data_domain ?? 'custom_extension',
                generic_target: editingType.generic_target ?? 'GEN_DOCUMENT_RECORD',
                coref_enabled: Boolean(editingType.coref_enabled),
              }
            : { use_llm: true, coref_enabled: true }
        }
        taxonomy={textTaxonomy}
        onSave={(form) => void handleSave(form)}
      />
      {confirmState && (
        <ConfirmDialog
          open
          title={confirmState.title}
          message={confirmState.message}
          danger={confirmState.danger}
          onConfirm={() =>
            void runLocked(async () => {
              setConfirmState(null);
              await confirmState.onConfirm();
            })
          }
          onCancel={() => setConfirmState(null)}
        />
      )}
      <InteractionLockOverlay active={operationLoading || saving} />
    </>
  );
}
