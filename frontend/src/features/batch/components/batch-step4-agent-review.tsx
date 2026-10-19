// Copyright 2026 LexMask Contributors

// 供料模式（原「喂Agent」）批量第 4 步：逐份映射表确认（Issue#196）。
// 审阅对象是映射表本身（D2 拍板：不做页面级预览）；交互与单文件供料
// step-review 同款（保留原文 / 自定义替换值 / 缺省=占位符替换），
// 决策经 review-draft（agent_md_decisions）自动保存，确认走 commit。

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useT } from '@/i18n';
import { cn } from '@/lib/utils';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Input } from '@/components/ui/input';
import { Card, CardContent } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { useAuth } from '@/features/auth/auth-context';
import { tonePanelClass } from '@/utils/toneClasses';
import {
  buildDecisions,
  type Decision,
  type MappingItemDto,
} from '@/features/agent-md/lib/agent-md-flow';
import {
  commitAllReviews,
  commitItemReview,
  getAgentMdMapping,
  putItemReviewDraft,
} from '@/services/jobsApi';

import { useBatchWizardContext } from '../batch-wizard-context';
import { RECOGNITION_DONE_STATUSES, type BatchRow } from '../types';

function reviewableAgentRows(rows: BatchRow[]): BatchRow[] {
  return rows.filter(
    (row) =>
      RECOGNITION_DONE_STATUSES.has(row.analyzeStatus) &&
      row.analyzeStatus !== 'failed' &&
      Boolean(row.item_id),
  );
}

export function BatchStep4AgentReview() {
  const t = useT();
  const { status } = useAuth();
  const canBulkConfirm = status?.can_bulk_confirm === true;
  const w = useBatchWizardContext();
  const { rows, activeJobId, refreshRowsFromActiveJob, goStep } = w;

  const agentRows = useMemo(() => reviewableAgentRows(rows), [rows]);
  const [index, setIndex] = useState(0);
  const current = agentRows[Math.min(index, Math.max(0, agentRows.length - 1))];

  const [mapping, setMapping] = useState<MappingItemDto[]>([]);
  const [edits, setEdits] = useState<Record<string, Decision>>({});
  const [loadState, setLoadState] = useState<'idle' | 'loading' | 'ready' | 'error'>('idle');
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [bulkConfirmOpen, setBulkConfirmOpen] = useState(false);
  const [bulkConfirmLoading, setBulkConfirmLoading] = useState(false);

  const mappingKeyRef = useRef<string>('');
  const autosaveTimerRef = useRef<number | null>(null);
  const decisionsDirtyRef = useRef(false);

  const decisions = useMemo(() => buildDecisions(mapping, edits), [mapping, edits]);

  const loadMapping = useCallback(
    async (jobId: string, itemId: string) => {
      const key = `${jobId}:${itemId}`;
      mappingKeyRef.current = key;
      setLoadState('loading');
      setLoadError(null);
      try {
        const out = await getAgentMdMapping(jobId, itemId);
        if (mappingKeyRef.current !== key) return;
        setMapping(out.items ?? []);
        setLoadState('ready');
      } catch (error) {
        if (mappingKeyRef.current !== key) return;
        setMapping([]);
        setLoadState('error');
        setLoadError(error instanceof Error ? error.message : String(error));
      }
    },
    [],
  );

  useEffect(() => {
    if (!activeJobId || !current?.item_id) return;
    setEdits({});
    decisionsDirtyRef.current = false;
    void loadMapping(activeJobId, current.item_id);
  }, [activeJobId, current?.item_id, loadMapping]);

  useEffect(
    () => () => {
      if (autosaveTimerRef.current !== null) window.clearTimeout(autosaveTimerRef.current);
    },
    [],
  );

  const flushDraft = useCallback(async () => {
    if (!activeJobId || !current?.item_id || !decisionsDirtyRef.current) return true;
    if (autosaveTimerRef.current !== null) {
      window.clearTimeout(autosaveTimerRef.current);
      autosaveTimerRef.current = null;
    }
    setSaving(true);
    setSaveError(null);
    try {
      await putItemReviewDraft(activeJobId, current.item_id, {
        entities: [],
        bounding_boxes: [],
        agent_md_decisions: decisions,
      });
      decisionsDirtyRef.current = false;
      return true;
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
      return false;
    } finally {
      setSaving(false);
    }
  }, [activeJobId, current?.item_id, decisions]);

  const scheduleAutosave = useCallback(() => {
    if (autosaveTimerRef.current !== null) window.clearTimeout(autosaveTimerRef.current);
    autosaveTimerRef.current = window.setTimeout(() => void flushDraft(), 800);
  }, [flushDraft]);

  const setEdit = useCallback(
    (id: string, decision: Decision) => {
      setEdits((prev) => ({ ...prev, [id]: decision }));
      decisionsDirtyRef.current = true;
      scheduleAutosave();
    },
    [scheduleAutosave],
  );

  const toggleExclude = useCallback(
    (item: MappingItemDto) => {
      const prev = edits[item.id];
      const nowExcluded = prev ? prev.action !== 'exclude' : !item.excluded;
      setEdit(item.id, nowExcluded ? { id: item.id, action: 'exclude' } : { id: item.id, action: 'keep' });
    },
    [edits, setEdit],
  );

  const setCustomReplacement = useCallback(
    (item: MappingItemDto, value: string) => {
      setEdit(item.id, { id: item.id, action: 'custom', replacement: value });
    },
    [setEdit],
  );

  const effectiveState = useCallback(
    (item: MappingItemDto): { excluded: boolean; replacement: string } => {
      const edit = edits[item.id];
      if (!edit) return { excluded: item.excluded, replacement: item.replacement };
      if (edit.action === 'exclude') return { excluded: true, replacement: item.replacement };
      if (edit.action === 'custom')
        return { excluded: false, replacement: edit.replacement ?? item.replacement };
      return { excluded: false, replacement: item.replacement };
    },
    [edits],
  );

  const confirmCurrent = useCallback(async () => {
    if (!activeJobId || !current?.item_id) return;
    setConfirming(true);
    setSaveError(null);
    try {
      const ok = await flushDraft();
      if (!ok) return;
      await commitItemReview(activeJobId, current.item_id, {
        entities: [],
        bounding_boxes: [],
        agent_md_decisions: decisions,
      });
      await refreshRowsFromActiveJob(activeJobId);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setConfirming(false);
    }
  }, [activeJobId, current?.item_id, decisions, flushDraft, refreshRowsFromActiveJob]);

  const bulkConfirm = useCallback(async () => {
    if (!activeJobId) return;
    setBulkConfirmLoading(true);
    try {
      await commitAllReviews(activeJobId);
      await refreshRowsFromActiveJob(activeJobId);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : String(error));
    } finally {
      setBulkConfirmLoading(false);
    }
  }, [activeJobId, refreshRowsFromActiveJob]);

  // 审阅键盘流与普通批量一致：← / → 切文件，Ctrl+Enter 确认当前份。
  const keyRef = useRef<(event: KeyboardEvent) => void>(() => {});
  keyRef.current = (event: KeyboardEvent) => {
    const target = event.target as HTMLElement | null;
    if (
      target &&
      (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)
    ) {
      return;
    }
    if (event.key === 'ArrowLeft' && index > 0) {
      event.preventDefault();
      void flushDraft().then((ok) => {
        if (ok) setIndex(index - 1);
      });
      return;
    }
    if (event.key === 'ArrowRight' && index < agentRows.length - 1) {
      event.preventDefault();
      void flushDraft().then((ok) => {
        if (ok) setIndex(index + 1);
      });
      return;
    }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey) && current) {
      event.preventDefault();
      void confirmCurrent();
    }
  };
  useEffect(() => {
    const listener = (event: KeyboardEvent) => keyRef.current(event);
    window.addEventListener('keydown', listener);
    return () => window.removeEventListener('keydown', listener);
  }, []);

  if (!agentRows.length) {
    return (
      <Card
        className="page-surface border-border/70 shadow-[var(--shadow-control)]"
        data-testid="batch-step4-agent-empty"
      >
        <CardContent className="space-y-3 p-6">
          <p className="text-sm text-muted-foreground">{t('batchWizard.step4.noRecognized')}</p>
        </CardContent>
      </Card>
    );
  }

  const confirmedCount = rows.filter((row) => row.analyzeStatus === 'completed').length;
  const pendingCount = agentRows.filter((row) => row.analyzeStatus === 'awaiting_review').length;
  const currentReadOnly = current.analyzeStatus === 'completed';

  return (
    <Card
      className="page-surface flex min-h-0 flex-1 flex-col overflow-hidden border-border/70 shadow-[var(--shadow-control)]"
      data-testid="batch-step4-agent-review"
    >
      <div className="shrink-0 border-b bg-muted/30 px-3 py-2" data-testid="agent-review-file-toolbar">
        <div className="flex min-w-0 flex-nowrap items-center gap-2 overflow-x-auto pb-0.5">
          <div className="min-w-0 flex-1">
            <span className="block truncate text-xs font-semibold" title={current.original_filename}>
              {current.original_filename}
            </span>
          </div>
          {agentRows.length > 1 && (
            <div className="flex shrink-0 items-center gap-1">
              <Button
                variant="outline"
                size="sm"
                className="h-8 whitespace-nowrap"
                disabled={index <= 0 || confirming}
                onClick={() => void flushDraft().then((ok) => ok && setIndex(index - 1))}
                data-testid="agent-review-prev"
              >
                {t('batchWizard.step4.prevFile')}
              </Button>
              <span className="min-w-12 text-center text-xs text-muted-foreground tabular-nums">
                {Math.min(index, agentRows.length - 1) + 1}/{agentRows.length}
              </span>
              <Button
                variant="outline"
                size="sm"
                className="h-8 whitespace-nowrap"
                disabled={index >= agentRows.length - 1 || confirming}
                onClick={() => void flushDraft().then((ok) => ok && setIndex(index + 1))}
                data-testid="agent-review-next"
              >
                {t('batchWizard.step4.nextFile')}
              </Button>
            </div>
          )}
          <div className="min-w-28 shrink-0 text-right">
            {saving && (
              <span className="whitespace-nowrap text-xs text-muted-foreground">
                {t('batchWizard.step4.savingDraft')}
              </span>
            )}
            {!saving && saveError && (
              <span className="block truncate text-xs text-destructive" title={saveError}>
                {saveError}
              </span>
            )}
            {!saving && !saveError && (
              <span className="whitespace-nowrap text-xs text-[var(--success-foreground)]">
                {t('batchWizard.step4.draftSynced')}
              </span>
            )}
          </div>
        </div>
      </div>

      <div className="shrink-0 border-b bg-muted/20 px-3 py-2">
        <p className="text-xs leading-5 text-muted-foreground">{t('batchWizard.step4Agent.hint')}</p>
      </div>

      {loadState === 'loading' && (
        <div className="flex min-h-[280px] flex-1 items-center justify-center" data-testid="agent-review-loading">
          <span className="size-6 animate-spin rounded-full border-2 border-border border-t-primary" />
        </div>
      )}
      {loadState === 'error' && (
        <div className="flex min-h-[200px] flex-1 flex-col items-center justify-center gap-3 p-6">
          <div className={`rounded-xl border px-4 py-3 text-sm ${tonePanelClass.danger}`}>
            {loadError || t('agentMd.mappingFailed')}
          </div>
          {current.item_id && activeJobId && (
            <Button
              size="sm"
              variant="outline"
              onClick={() => void loadMapping(activeJobId, current.item_id as string)}
              data-testid="agent-review-retry"
            >
              {t('common.retry')}
            </Button>
          )}
        </div>
      )}
      {loadState === 'ready' && (
        <div className="min-h-0 flex-1 overflow-auto p-3">
          {mapping.length === 0 ? (
            <p className="p-6 text-sm text-muted-foreground" data-testid="agent-review-empty-mapping">
              {t('agentMd.mappingEmpty')}
            </p>
          ) : (
            <table className="w-full border-collapse text-sm" data-testid="agent-review-mapping-table">
              <thead>
                <tr className="border-b text-left text-xs text-muted-foreground">
                  <th className="py-2 pr-3 font-medium">{t('agentMd.colOriginal')}</th>
                  <th className="py-2 pr-3 font-medium">{t('agentMd.colType')}</th>
                  <th className="py-2 pr-3 font-medium">{t('agentMd.colReplacement')}</th>
                  <th className="py-2 text-right font-medium">{t('agentMd.exclude')}</th>
                </tr>
              </thead>
              <tbody>
                {mapping.map((item) => {
                  const state = effectiveState(item);
                  return (
                    <tr key={item.id} className="border-b last:border-0" data-testid={`agent-mapping-row-${item.id}`}>
                      <td className="max-w-64 truncate py-2 pr-3" title={item.original_text}>
                        {item.original_text}
                      </td>
                      <td className="py-2 pr-3">
                        <Badge variant="outline" className="rounded-full px-2 py-0.5 text-[11px]">
                          {item.entity_type}
                        </Badge>
                      </td>
                      <td className="py-2 pr-3">
                        <Input
                          value={state.excluded ? '' : state.replacement}
                          disabled={currentReadOnly || state.excluded}
                          onChange={(e) => setCustomReplacement(item, e.target.value)}
                          className="h-8 max-w-56"
                          data-testid={`agent-mapping-replacement-${item.id}`}
                        />
                      </td>
                      <td className="py-2 text-right">
                        <label className="inline-flex cursor-pointer items-center gap-1.5 text-xs">
                          <input
                            type="checkbox"
                            checked={state.excluded}
                            disabled={currentReadOnly}
                            onChange={() => toggleExclude(item)}
                            data-testid={`agent-mapping-exclude-${item.id}`}
                          />
                          {state.excluded ? t('agentMd.exclude') : t('agentMd.include')}
                        </label>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </div>
      )}

      <div
        className="sticky bottom-0 z-10 shrink-0 border-t bg-background/95 px-3 py-2 backdrop-blur-xl"
        data-testid="agent-review-action-bar"
      >
        <div className="flex flex-wrap items-center justify-end gap-2">
          <span className="mr-auto shrink-0 whitespace-nowrap text-xs text-muted-foreground tabular-nums">
            {t('batchWizard.step4.confirmed')} {confirmedCount}/{rows.length}
          </span>
          {pendingCount > 0 && (
            <span
              className="shrink-0"
              title={!canBulkConfirm ? t('batchWizard.step4.bulkConfirmNoPermission') : undefined}
            >
              <Button
                type="button"
                size="sm"
                variant="outline"
                className="whitespace-nowrap"
                disabled={!canBulkConfirm || confirming || bulkConfirmLoading}
                onClick={() => setBulkConfirmOpen(true)}
                data-testid="agent-bulk-confirm-all"
              >
                {bulkConfirmLoading
                  ? t('batchWizard.step4.bulkConfirming')
                  : t('batchWizard.step4.bulkConfirmAll').replace('{count}', String(pendingCount))}
              </Button>
            </span>
          )}
          <Button
            size="sm"
            className="shrink-0 whitespace-nowrap"
            onClick={() => void confirmCurrent()}
            disabled={confirming || saving || currentReadOnly || loadState !== 'ready'}
            data-testid="agent-confirm-generate"
          >
            {confirming
              ? t('batchWizard.step4.submitting')
              : currentReadOnly
                ? t('batchWizard.step4.completed')
                : t('batchWizard.step4Agent.confirmGenerate')}
          </Button>
          <Button
            size="sm"
            variant={w.canAdvanceToExport ? 'default' : 'outline'}
            disabled={!w.canAdvanceToExport}
            onClick={() => goStep(5)}
            className={cn('shrink-0 whitespace-nowrap', w.canAdvanceToExport && 'bg-primary hover:bg-primary/90')}
            data-testid="agent-go-export"
          >
            {t('batchWizard.step4.goExport')}
          </Button>
        </div>
      </div>

      <ConfirmDialog
        open={bulkConfirmOpen}
        title={t('batchWizard.step4.bulkConfirmTitle')}
        message={t('batchWizard.step4.bulkConfirmMessage').replace('{count}', String(pendingCount))}
        confirmText={t('batchWizard.step4.bulkConfirmConfirm')}
        onConfirm={() => {
          setBulkConfirmOpen(false);
          void bulkConfirm();
        }}
        onCancel={() => setBulkConfirmOpen(false)}
      />
    </Card>
  );
}
