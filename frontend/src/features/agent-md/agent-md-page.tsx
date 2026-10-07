// Copyright 2026 LexMask Contributors

import { useCallback, useState } from 'react';
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

/** Issue#75 喂 Agent：/agent-md 四步动线（上传→解析→复核映射→出稿）。 */
export function AgentMd() {
  const t = useT();
  const [step, setStep] = useState<FlowStep>('upload');
  const [taskId, setTaskId] = useState<string | null>(null);
  const [status, setStatus] = useState<AgentMdStatus | null>(null);
  const [items, setItems] = useState<MappingItemDto[]>([]);
  const [error, setError] = useState<string | null>(null);

  // 错误横幅只承载「当前步骤不可见」的瞬时错误，步骤推进即清（评审 R1：防陈旧横幅跟随全流程）
  const onUploaded = useCallback((id: string) => {
    setError(null);
    setTaskId(id);
    setStep('processing');
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
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 p-6" data-testid="agent-md-page">
      <h1 className="text-xl font-semibold tracking-tight">{t('agentMd.title')}</h1>
      {error && (
        <div className="rounded-lg border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm font-medium text-destructive">
          {error}
        </div>
      )}
      {step === 'upload' && <StepUpload onUploaded={onUploaded} />}
      {step === 'processing' && (
        <StepProcessing
          status={status}
          pollOnce={pollOnce}
          onError={setError}
          onFail={() => {
            // 清掉上一任务的终态与横幅，避免重传后首轮轮询前闪现旧 failed 面板/错误
            setStatus(null);
            setError(null);
            setStep('upload');
          }}
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
