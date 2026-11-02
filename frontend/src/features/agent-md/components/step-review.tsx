// Copyright 2026 LexMask Contributors

import { useEffect, useState } from 'react';
import { useT } from '@/i18n';
import { getEntityTypeName } from '@/config/entityTypes';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { needsZeroEntityAck, type MappingItemDto, type ItemEdits } from '../lib/agent-md-flow';

/** 每页固定 20 行（YAGNI：不做行数选择器）。 */
const PAGE_SIZE = 20;

interface StepReviewProps {
  items: MappingItemDto[];
  /**
   * 确认生成：页面把 edits 经 buildDecisions 转决策并 POST /confirm。
   * Issue#212：0 实体时须带上知情确认（acknowledgeZeroEntities）。
   */
  onConfirm: (edits: ItemEdits, options?: { acknowledgeZeroEntities?: boolean }) => Promise<void>;
}

/**
 * 四步动线第三步：映射表复核。
 * 行语义对齐后端决策（agent_md_replace.apply_decisions）：
 * 勾选「保留原文」→ action=exclude；取消勾选 → action=keep（按映射替换值替换）；
 * 改写替换值 → action=custom。只把用户碰过的行写进本地 edits（buildDecisions 只上送改动项）。
 */
export function StepReview({ items, onConfirm }: StepReviewProps) {
  const t = useT();
  const [edits, setEdits] = useState<ItemEdits>({});
  const [confirming, setConfirming] = useState(false);
  const [issue, setIssue] = useState<string | null>(null);
  // Issue#212：0 实体出稿闸——空映射时默认不可生成，须显式勾选知情
  const [zeroAck, setZeroAck] = useState(false);
  // 分页：edits 按 item.id 键控，与页码无关，翻页不丢改动
  const [page, setPage] = useState(1);
  const pageCount = Math.max(1, Math.ceil(items.length / PAGE_SIZE));

  // 新任务（items 变化）时回到第一页，并夹紧越界页码
  useEffect(() => {
    setPage(1);
  }, [items]);

  // 新任务（items 变化）时重置知情勾选，避免沿用上一份文档的确认
  useEffect(() => {
    setZeroAck(false);
  }, [items]);

  const zeroEntities = needsZeroEntityAck(items);

  const safePage = Math.min(page, pageCount);
  const pageItems = items.slice((safePage - 1) * PAGE_SIZE, safePage * PAGE_SIZE);

  const setEdit = (id: string, edit: ItemEdits[string] | undefined) => {
    setEdits((prev) => {
      const next = { ...prev };
      if (edit === undefined) delete next[id];
      else next[id] = edit;
      return next;
    });
  };

  const confirm = async () => {
    if (confirming) return;
    if (zeroEntities && !zeroAck) return; // 未勾选知情不发请求（按钮本身 disabled）
    setConfirming(true);
    setIssue(null);
    try {
      await onConfirm(edits, { acknowledgeZeroEntities: zeroEntities && zeroAck });
    } catch (err) {
      setIssue(err instanceof Error && err.message ? err.message : t('agentMd.confirmFailed'));
    } finally {
      setConfirming(false);
    }
  };

  return (
    <section className="flex flex-col gap-4" data-testid="agent-md-step-review">
      <h2 className="text-lg font-semibold tracking-tight">{t('agentMd.reviewTitle')}</h2>

      {items.length === 0 ? (
        <div className="rounded-2xl border border-border bg-card p-8 text-center">
          <p className="text-sm text-muted-foreground">{t('agentMd.mappingEmpty')}</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-2xl border border-border bg-card">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[38%]">{t('agentMd.colOriginal')}</TableHead>
                <TableHead className="w-[16%]">{t('agentMd.colType')}</TableHead>
                <TableHead>{t('agentMd.colReplacement')}</TableHead>
                <TableHead className="w-[7.5rem] text-center">{t('agentMd.exclude')}</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {pageItems.map((item, i) => {
                const index = (safePage - 1) * PAGE_SIZE + i;
                const edit = edits[item.id];
                const excluded = edit ? edit.action === 'exclude' : item.excluded;
                const replacementValue =
                  edit?.action === 'custom' ? edit.replacement ?? '' : item.replacement;
                return (
                  <TableRow key={item.id} data-testid={`agent-md-row-${index}`}>
                    <TableCell className="max-w-0 truncate font-medium" title={item.original_text}>
                      {item.original_text}
                    </TableCell>
                    <TableCell className="text-muted-foreground">
                      {getEntityTypeName(item.entity_type)}
                    </TableCell>
                    <TableCell>
                      <Input
                        value={excluded ? '' : replacementValue}
                        onChange={(e) =>
                          setEdit(item.id, { action: 'custom', replacement: e.target.value })
                        }
                        placeholder={t('agentMd.customPlaceholder')}
                        disabled={confirming || excluded}
                        className="h-9"
                        aria-label={`${t('agentMd.colReplacement')}: ${item.original_text}`}
                        data-testid={`agent-md-replacement-${index}`}
                      />
                    </TableCell>
                    <TableCell className="text-center">
                      <Checkbox
                        checked={excluded}
                        onCheckedChange={(checked) =>
                          setEdit(item.id, checked === true ? { action: 'exclude' } : { action: 'keep' })
                        }
                        disabled={confirming}
                        aria-label={`${t('agentMd.exclude')}: ${item.original_text}`}
                        data-testid={`agent-md-exclude-${index}`}
                      />
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

      {items.length > PAGE_SIZE && (
        <div
          className="flex items-center justify-center gap-3 text-sm text-muted-foreground"
          data-testid="agent-md-pager"
        >
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage(safePage - 1)}
            disabled={safePage <= 1}
            data-testid="agent-md-pager-prev"
          >
            {t('agentMd.pagePrev')}
          </Button>
          <span data-testid="agent-md-pager-info">
            {t('agentMd.pageInfo')
              .replace('{x}', String(safePage))
              .replace('{y}', String(pageCount))}
          </span>
          <Button
            variant="outline"
            size="sm"
            onClick={() => setPage(safePage + 1)}
            disabled={safePage >= pageCount}
            data-testid="agent-md-pager-next"
          >
            {t('agentMd.pageNext')}
          </Button>
        </div>
      )}

      {zeroEntities && (
        <div
          className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive"
          role="alert"
          data-testid="agent-md-zero-entities-warning"
        >
          {t('agentMd.zeroEntitiesWarning')}
        </div>
      )}

      {zeroEntities && (
        <label
          className="flex items-start gap-2 text-sm text-muted-foreground"
          data-testid="agent-md-zero-ack-label"
        >
          <Checkbox
            checked={zeroAck}
            onCheckedChange={(checked) => setZeroAck(checked === true)}
            disabled={confirming}
            aria-label={t('agentMd.zeroEntitiesAck')}
            data-testid="agent-md-zero-ack"
          />
          <span>{t('agentMd.zeroEntitiesAck')}</span>
        </label>
      )}

      {issue && (
        <div
          className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive"
          role="alert"
          data-testid="agent-md-confirm-issue"
        >
          {issue}
        </div>
      )}

      <div className="flex justify-end">
        <Button
          onClick={() => void confirm()}
          disabled={confirming || (zeroEntities && !zeroAck)}
          data-testid="agent-md-confirm"
        >
          {confirming ? '…' : t('agentMd.confirmGenerate')}
        </Button>
      </div>
    </section>
  );
}
