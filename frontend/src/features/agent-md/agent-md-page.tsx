// Copyright 2026 LexMask Contributors

import { useCallback, useEffect, useRef, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useT } from '@/i18n';
import { agentMdApi, type AgentMdStatus } from './api';
import {
  buildDecisions,
  nextStepFromStatus,
  type FlowStep,
  type ItemEdits,
  type MappingItemDto,
} from './lib/agent-md-flow';
import { StepUpload } from './components/step-upload';
import { StepProcessing } from './components/step-processing';
import { StepReview } from './components/step-review';
import { StepResult } from './components/step-result';

/** 活跃任务 localStorage 键：跨页面跳转后恢复动线（用户反馈「切页面状态全丢」）。 */
const ACTIVE_TASK_KEY = 'agent-md.active-task';
/** 可恢复的状态：终态 failed 不恢复（404/failed 由调用处清键）。 */
const RESUMABLE_STATES = new Set(['parsing', 'ner_running', 'mapping_ready', 'completed']);

/** Issue#75 喂 Agent：/agent-md 四步动线（上传→解析→复核映射→出稿）。 */
export function AgentMd() {
  const t = useT();
  const [searchParams, setSearchParams] = useSearchParams();
  const [step, setStep] = useState<FlowStep>('upload');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState<AgentMdStatus | null>(null);
  const [items, setItems] = useState<MappingItemDto[]>([]);
  const [error, setError] = useState<string | null>(null);
  // 挂载恢复只跑一次：fileId 分流参数 / localStorage 活跃任务
  const bootstrapped = useRef(false);

  // 错误横幅只承载「当前步骤不可见」的瞬时错误，步骤推进即清（评审 R1：防陈旧横幅跟随全流程）
  const onUploaded = useCallback((id: string) => {
    setError(null);
    setTaskId(id);
    setStep('processing');
  }, []);

  const startTask = useCallback(
    (id: string) => {
      localStorage.setItem(ACTIVE_TASK_KEY, id);
      onUploaded(id);
    },
    [onUploaded],
  );

  const resetToUpload = useCallback(() => {
    localStorage.removeItem(ACTIVE_TASK_KEY);
    // 清掉上一任务的终态与横幅，避免重传后首轮轮询前闪现旧 failed 面板/错误
    setStatus(null);
    setError(null);
    setTaskId(null);
    setStep('upload');
  }, []);

  // 挂载恢复：?fileId= 分流（playground 入口，免重传）→ 直接建任务；
  // 否则读 localStorage 活跃任务，状态可续（解析/NER/评审/出稿）则恢复到对应步骤。
  useEffect(() => {
    if (bootstrapped.current) return;
    bootstrapped.current = true;
    let cancelled = false;
    void (async () => {
      const fileId = searchParams.get('fileId');
      const filename = searchParams.get('filename');
      if (fileId) {
        // 先清参数再恢复，防止返回/刷新时重复建任务
        setSearchParams({}, { replace: true });
        try {
          const { task_id } = await agentMdApi.startFromFileId(fileId, filename ?? undefined);
          if (!cancelled) startTask(task_id);
        } catch (err) {
          if (!cancelled) {
            setError(err instanceof Error && err.message ? err.message : t('agentMd.uploadFailed'));
          }
        }
        return;
      }
      const stored = localStorage.getItem(ACTIVE_TASK_KEY);
      if (!stored) return;
      try {
        const st = await agentMdApi.status(stored);
        if (!RESUMABLE_STATES.has(st.state)) {
          localStorage.removeItem(ACTIVE_TASK_KEY);
          return;
        }
        if (!cancelled) {
          onUploaded(stored);
          setStatus(st); // StepProcessing 首轮 pollOnce 会按状态切到 review/result
        }
      } catch {
        // 404/网络失败：任务不存在，清键回到上传步
        localStorage.removeItem(ACTIVE_TASK_KEY);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 失败态（state=failed）由 nextStepFromStatus 留在 processing，由 StepProcessing 展示 + onFail 回上传
  const pollOnce = useCallback(async (): Promise<AgentMdStatus | null> => {
    if (!taskId) return null;
    const st = await agentMdApi.status(taskId);
    setStatus(st);
    const next = nextStepFromStatus(st.state);
    if (next === 'review') {
      const m = await agentMdApi.mapping(taskId);
      setItems(m.items);
    }
    if (next !== 'processing') setError(null); // 轮询成功推进（评审/出稿）即清横幅
    setStep(next);
    return st;
  }, [taskId]);

  return (
    // Layout 的 main 是固定高 + overflow-hidden（playground 等自管滚动），本页需自带滚动容器，
    // 否则长页面（评审 197 行映射表）被裁切且无法滑动（用户验收反馈）。
    <div
      className="mx-auto flex w-full max-w-4xl min-h-0 flex-1 flex-col gap-6 overflow-y-auto p-6"
      data-testid="agent-md-page"
    >
      <h1 className="text-xl font-semibold tracking-tight">{t('agentMd.title')}</h1>
      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive">
          {error}
        </div>
      )}
      {step === 'upload' && <StepUpload onUploaded={startTask} />}
      {step === 'processing' && (
        <StepProcessing
          status={status}
          pollOnce={pollOnce}
          onError={setError}
          onFail={resetToUpload}
        />
      )}
      {step === 'review' && taskId && (
        <StepReview
          items={items}
          onConfirm={async (edits: ItemEdits) => {
            const decisions = buildDecisions(items, edits);
            await agentMdApi.confirm(taskId, decisions);
            setStep('result');
          }}
        />
      )}
      {step === 'result' && taskId && <StepResult taskId={taskId} />}
    </div>
  );
}

export default AgentMd;
