// Copyright 2026 LexMask Contributors
// @vitest-environment jsdom

// Issue#351 P1-B/C（v2 方案）：批量中转页导航收敛 + 向导双步骤条。
//
// B1 删 JourneyRail（01/02/03 三步引导卡）；B2 主入口卡 journeyNote 删除+按钮差异化；
// C1 删文字步骤行（stepsOverview 正文清零，图形步骤条 aria-label 承载）。
//
// 钉法（冷审修订）：负向断言均配正向/role 断言——按钮用 getByRole 验证在场+可点，
// 步骤条正向钉 aria-label 值本身；「无 X」断言旁必有一处「有 Y」防打空。

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useI18n } from '@/i18n';

const wizardState = vi.hoisted(() => ({
  step: 1,
  msg: null,
  mode: 'smart',
}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({ health: null, checking: false }),
}));
vi.mock('./hooks/use-batch-hub', () => ({
  useBatchHub: () => ({
    activeJobs: [],
    loading: false,
    tableLoading: false,
    continueJob: vi.fn(),
    openBatch: vi.fn(),
    openPreview: vi.fn(),
  }),
}));
vi.mock('./hooks/use-batch-wizard', () => ({
  useBatchWizard: () => ({
    mode: 'smart',
    modeValid: true,
    step: wizardState.step,
    msg: null,
    canGoStep: (s: number) => s <= wizardState.step,
    goStep: vi.fn(),
    // step1 配置组件消费面（cfg/jobConfigLocked 等）——最小可渲染集
    textPresets: [],
    visionPresets: [],
    entityTypes: [],
    visionTypes: [],
    textTypes: [],
    pipelines: [],
    ocrHasTypes: [],
    ocrTypes: [],
    visualFeatureTypes: [],
    configLoaded: true,
    cfg: {
      executionDefault: 'queue',
      replacementMode: 'structured',
      imageRedactionMethod: 'mosaic',
      imageRedactionStrength: 75,
      selectedEntityTypeIds: [],
      selectedOcrHasTypeIds: [],
      selectedVisionTypeIds: [],
    },
    setCfg: vi.fn(),
    jobConfigLocked: false,
    presetLoadError: null,
    presetReloading: false,
    retryLoadPresets: vi.fn(),
    onBatchTextPresetChange: vi.fn(),
    onBatchVisionPresetChange: vi.fn(),
    confirmStep1: false,
    setConfirmStep1: vi.fn(),
    isStep1Complete: false,
    advanceToUploadStep: vi.fn(),
    previewMode: false,
    activeJobId: null,
    rows: [],
    loading: false,
    isDragActive: false,
    getRootProps: () => ({}),
    getInputProps: () => ({}),
    uploadIssues: [],
    uploadProgress: {},
    clearUploadIssues: vi.fn(),
    failedUploadCount: 0,
    retryFailedUploads: vi.fn(),
    removeRow: vi.fn(),
    clearRows: vi.fn(),
    submitQueueToWorker: vi.fn(),
    requeueFailedItems: vi.fn(),
    resolveExportIssue: vi.fn(),
    flushCurrentReviewDraft: vi.fn(),
    failedRows: [],
    refreshRowsFromActiveJob: vi.fn(),
  }),
}));

import { BatchHub } from './batch-hub';
import { BatchWizard } from './batch-wizard';

beforeEach(() => {
  // locale 锁 zh：测试环境默认 locale=en（resolveInitialLocale），标题走 en fallback
  useI18n.setState({ locale: 'zh' });
});

afterEach(() => {
  cleanup();
  wizardState.step = 1;
  vi.clearAllMocks();
});

describe('Issue#351 P1-B：批量中转页导航收敛', () => {
  it('B1：JourneyRail 三步卡不渲染（负向）；主入口与供料入口在场（正向）', () => {
    render(
      <MemoryRouter>
        <BatchHub />
      </MemoryRouter>,
    );
    // 负向：journey rail 与其三步文案清零
    expect(screen.queryByTestId('batch-journey-rail')).toBeNull();
    expect(screen.queryByText('大多数用户应先从混合文件开始。')).toBeNull();
    // 正向：两个入口的 role 按钮在场且可点
    const batchBtn = screen.getByRole('button', { name: '开始批量处理' });
    const agentBtn = screen.getByRole('button', { name: '开始供料' });
    expect(batchBtn).toBeTruthy();
    expect(agentBtn).toBeTruthy();
    // 正向：入口卡主内容仍在（收敛≠功能删减）
    expect(screen.getByText('处理多个文件')).toBeTruthy();
    expect(screen.getByText('供料模式')).toBeTruthy();
  });

  it('B2：按钮文案与卡标题差异化（按钮=开始批量处理 ≠ 标题=处理多个文件）', () => {
    render(
      <MemoryRouter>
        <BatchHub />
      </MemoryRouter>,
    );
    const batchBtn = screen.getByRole('button', { name: '开始批量处理' });
    expect(batchBtn.textContent).toContain('开始批量处理');
    expect(batchBtn.textContent).not.toContain('处理多个文件');
    // 标题本身未被改（信息在标题，动作在按钮）
    expect(screen.getByText('处理多个文件')).toBeTruthy();
  });
});

describe('Issue#351 P1-C：向导步骤条与长句', () => {
  it('C1：文字步骤行删除（负向）；图形步骤条在场且 aria-label=stepsOverview 文案（正向）', () => {
    render(
      <MemoryRouter initialEntries={['/batch/smart?jobId=j-1&step=1']}>
        <BatchWizard />
      </MemoryRouter>,
    );
    // 负向：正文不再有「五步：…」文字行
    expect(screen.queryByText(/五步：选择清单/)).toBeNull();
    // 正向：图形步骤条在场，aria-label 承载步骤总览（值本身被钉）
    const progress = screen.getByLabelText('五步：选择清单 → 上传 → 批量识别 → 审阅确认 → 导出');
    expect(progress).toBeTruthy();
    expect(progress.getAttribute('data-testid')).toBe('batch-step-progress');
    // 正向：步骤条按钮渲染（5 步）
    const stepButtons = progress.querySelectorAll('button');
    expect(stepButtons.length).toBe(5);
  });

  it('C2：step1 说明为新短句（容器内钉，无「固定格式和语义识别」技术叙述）', () => {
    render(
      <MemoryRouter initialEntries={['/batch/smart?jobId=j-1&step=1']}>
        <BatchWizard />
      </MemoryRouter>,
    );
    // 负向：旧长句片段清零
    expect(screen.queryByText(/固定格式和语义识别哪些东西/)).toBeNull();
    // 正向：新短句在场
    expect(screen.getByText('配置本批次的识别项与处理方式，修改会自动保存。')).toBeTruthy();
  });
});