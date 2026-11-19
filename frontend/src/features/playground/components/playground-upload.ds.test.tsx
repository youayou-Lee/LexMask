// @vitest-environment jsdom
// Copyright 2026 LexMask Contributors

// Issue#355 Agent C：投料态 ds 分支钉（真稿 ios-single-upload：大上传页+小配置条）。
// - ds=产品哲学样板：小配置条（方案/保留水印/页码过滤/高级▸）+ 最近处理；
//   投料态零配置面板——无 preset chips、无标签墙、无 switch/checkbox（产品哲学钉，
//   规格 §七「投料态零配置面板」）；「高级」入口沿用 playground-scope-entry
//   testid/ARIA 契约打开识别范围三区弹窗。
// - classic 零新 DOM：无 config strip / recent / format chips。
// mock 惯例与 playground-upload.reskin.test.tsx 一致（rec 形状逐键对齐）。

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { resolveLegalTextSelection } from '@/config/legalPreset';
import { useI18n } from '@/i18n';
import { zh } from '@/i18n/zh';
import { useUiTheme } from '@/theme/ui-theme';
import type { ServicesHealth } from '@/hooks/use-service-health';
import type { usePlayground } from '../hooks/use-playground';
import type { EntityTypeConfig } from '../types';
import type { FileListResponse } from '@/types';
import pageSource from '../playground-page.tsx?raw';
import dropzoneSource from './playground-upload-dropzone.tsx?raw';
import uploadSource from './playground-upload.tsx?raw';
import { PlaygroundUpload } from './playground-upload';

// Issue#355 Agent C 新增键：编排者统一加进 src/i18n/zh.ts / en.ts（文案见本 Agent 报告）。
// 键就位前测试先按报告文案原地 seed（与编排者将写入的值逐字一致——文案钉）。
const PENDING_I18N_KEYS: Record<string, string> = {
  'playground.chooseFile': '选择文件',
  'playground.upload.autoFlow': '上传后自动识别 → 自动脱敏 → 完成后通知复核',
  'playground.upload.formatScan': '扫描件',
  'playground.upload.formatImage': '图片',
  'playground.upload.schemeLabel': '方案',
  'playground.upload.schemeDefault': '默认打码 · {count} 类实体',
  'playground.upload.watermarkKeep': '保留水印',
  'playground.upload.pageFilter': '页码过滤',
  'playground.upload.advancedSettings': '高级设置',
  'playground.upload.recentTitle': '最近处理',
  'playground.upload.recentResume': '继续复核',
  'playground.upload.recentResult': '查看结果',
  'playground.upload.historyLink': '识别历史',
  'playground.result.headSub': '匿名化完成 · {count} 个实体已处理 · 共 {pages} 页',
};

const mockHealthState = vi.hoisted(() => ({
  health: null as ServicesHealth | null,
  checking: false,
}));

const mockFilesState = vi.hoisted(() => ({
  list: vi.fn(),
}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({
    health: mockHealthState.health,
    checking: mockHealthState.checking,
    roundTripMs: null,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/services/hooks/use-presets', () => {
  const stablePresets: never[] = [];
  return {
    usePresets: () => ({ data: stablePresets, isLoading: false }),
    useInvalidatePresets: () => async () => {},
  };
});

// 最近处理：mock fileApi.list（只读 GET /files；组件契约=失败/为空整块不渲染）
vi.mock('@/services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api')>();
  return {
    ...actual,
    fileApi: {
      ...actual.fileApi,
      list: mockFilesState.list,
    },
  };
});

type PlaygroundApi = ReturnType<typeof usePlayground>;
type RecognitionCtx = PlaygroundApi['recognition'];

const CATALOG_A: EntityTypeConfig[] = [
  { id: 'PERSON', name: 'Person', color: '#000000', enabled: true, default_enabled: true },
  { id: 'PHONE', name: 'Phone', color: '#000000', enabled: true, default_enabled: true },
  { id: 'CASE_NUMBER', name: 'CaseNumber', color: '#000000', enabled: true, default_enabled: true },
  { id: 'MED_PATIENT', name: 'Patient', color: '#000000', enabled: true, default_enabled: false },
  { id: 'BANK_CARD', name: 'BankCard', color: '#000000', enabled: true, default_enabled: false },
];

const LEGAL_IDS = resolveLegalTextSelection(CATALOG_A.map((type) => type.id));

function makeHealth(allOnline: boolean): ServicesHealth {
  return {
    all_online: allOnline,
    services: {
      paddle_ocr: { name: 'PaddleOCR', status: 'online' },
      has_ner: { name: 'HaS Text', status: 'online' },
      visual_features: { name: 'visual', status: 'online' },
    },
  };
}

function makeRec(overrides: {
  entityTypes: EntityTypeConfig[];
  selectedTypes: string[];
  watermarkDelivery?: 'keep' | 'strip';
}): RecognitionCtx {
  return {
    entityTypes: overrides.entityTypes,
    textConfigState: 'ready',
    selectedTypes: overrides.selectedTypes,
    selectedTypesRef: { current: overrides.selectedTypes },
    setSelectedTypes: vi.fn(),
    visionTypes: [],
    visionConfigState: 'ready',
    selectedOcrHasTypes: [],
    selectedVisualFeatureTypes: [],
    selectedOcrHasTypesRef: { current: [] },
    selectedVisualFeatureTypesRef: { current: [] },
    pipelines: [],
    processingMode: 'mask',
    setProcessingMode: vi.fn(),
    replacementMode: 'mask',
    setReplacementMode: vi.fn(),
    watermarkText: '',
    setWatermarkText: vi.fn(),
    watermarkDelivery: overrides.watermarkDelivery ?? 'keep',
    setWatermarkDelivery: vi.fn(),
    textPresetsPg: [],
    visionPresetsPg: [],
    playgroundPresetTextId: null,
    playgroundPresetVisionId: null,
    selectPlaygroundTextPresetById: vi.fn(),
    selectPlaygroundVisionPresetById: vi.fn(),
    clearPlaygroundTextPresetTracking: vi.fn(),
    clearPlaygroundVisionPresetTracking: vi.fn(),
    toggleVisionType: vi.fn(),
    updateOcrHasTypes: vi.fn(),
    updateVisualFeatureTypes: vi.fn(),
    presetApplySeq: 0,
    getTypeConfig: vi.fn(),
    getVisionTypeConfig: vi.fn(),
  } as unknown as RecognitionCtx;
}

function makeCtx(rec: RecognitionCtx): PlaygroundApi {
  return {
    dropzone: {
      getRootProps: () => ({}),
      getInputProps: () => ({}),
      isDragActive: false,
      open: vi.fn(),
    },
    uploadIssue: null,
    recognition: rec,
  } as unknown as PlaygroundApi;
}

/** useSearchParams 探针：断言点击最近处理行走 ?file_id= 深链恢复协议 */
function LocationProbe() {
  const location = useLocation();
  return <span data-testid="location-probe">{location.search}</span>;
}

function renderUpload(ctx: PlaygroundApi) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <LocationProbe />
      <PlaygroundUpload ctx={ctx} />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
  useUiTheme.setState({ theme: 'ds' });
  // 编排者键就位前原地 seed（覆盖写入无害：同键同值）
  for (const [key, value] of Object.entries(PENDING_I18N_KEYS)) {
    if (!(key in zh)) (zh as Record<string, string>)[key] = value;
  }
  mockHealthState.health = makeHealth(true);
  mockFilesState.list.mockResolvedValue({ files: [], total: 0, page: 1, page_size: 4 });
});

afterEach(() => {
  cleanup();
  useUiTheme.setState({ theme: 'classic' });
  mockHealthState.health = null;
  mockFilesState.list.mockReset();
});

describe('Issue#355 投料态 ds：小配置条（默认值替选择）', () => {
  it('配置条在场：方案 chip=N 类实体（selectedTypes 实时数）+ ✓保留水印 + ✓页码过滤 + 高级入口', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = renderUpload(makeCtx(rec));

    const strip = container.querySelector('[data-testid="playground-config-strip"]');
    expect(strip).not.toBeNull();
    // 方案 chip：N=当前选中实体类数（法律默认 4）
    const scheme = container.querySelector('[data-testid="playground-scheme-chip"]');
    expect(scheme?.textContent).toBe('默认打码 · 4 类实体');
    // 保留水印 chip 存在（watermarkDelivery 默认 keep）
    expect(container.querySelector('[data-testid="playground-cfg-watermark"]')).not.toBeNull();
    // 页码过滤 chip 存在（后端默认闸的知情展示）
    expect(container.querySelector('[data-testid="playground-pagefilter-chip"]')).not.toBeNull();
    // 高级入口沿用既有 testid/ARIA 契约
    const entry = container.querySelector('[data-testid="playground-scope-entry"]');
    expect(entry?.getAttribute('aria-haspopup')).toBe('dialog');
    expect(entry?.getAttribute('aria-expanded')).toBe('false');
  });

  it('产品哲学钉：ds 投料态零配置面板——无 preset chips / 无标签墙 / 无 switch / 无 checkbox', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = renderUpload(makeCtx(rec));

    expect(container.querySelector('[data-testid="playground-preset-chip-legal"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-preset-chip-all"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-selected-tags"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-type-panel"]')).toBeNull();
    expect(container.querySelector('[role="switch"]')).toBeNull();
    expect(container.querySelector('input[type="checkbox"]')).toBeNull();
  });

  it('高级入口点击打开识别范围三区弹窗（功能平移进「高级」，不删功能）', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    renderUpload(makeCtx(rec));
    expect(screen.queryByTestId('playground-scope-dialog')).toBeNull();
    fireEvent.click(screen.getByTestId('playground-scope-entry'));
    expect(screen.queryByTestId('playground-scope-dialog')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-zone-ner')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-zone-ocr')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-zone-la')).not.toBeNull();
  });

  it('保留水印 chip 随 watermarkDelivery=strip 消失（不谎报默认值）', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS], watermarkDelivery: 'strip' });
    const { container } = renderUpload(makeCtx(rec));
    expect(container.querySelector('[data-testid="playground-cfg-watermark"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-pagefilter-chip"]')).not.toBeNull();
  });
});

describe('Issue#355 投料态 ds：最近处理（?file_id= 深链恢复协议）', () => {
  it('拉到 playground 源文件 → 渲染行 + has_output 分流「查看结果/继续复核」；点击行写 file_id 参数', async () => {
    mockFilesState.list.mockResolvedValue({
      files: [
        {
          file_id: 'f-done',
          original_filename: '第四卷.pdf',
          has_output: true,
          created_at: new Date().toISOString(),
        },
        {
          file_id: 'f-review',
          original_filename: '扫描型PDF误判为文本型-2.pdf',
          has_output: false,
          created_at: new Date().toISOString(),
        },
      ],
      total: 2,
      page: 1,
      page_size: 4,
    } as FileListResponse);
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = renderUpload(makeCtx(rec));

    const recent = await screen.findByTestId('playground-recent');
    expect(recent).not.toBeNull();
    expect(container.querySelector('[data-testid="playground-recent-row-f-done"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="playground-recent-row-f-review"]')).not.toBeNull();
    // 只拉 playground 源、限额 4 条
    expect(mockFilesState.list).toHaveBeenCalledWith(1, 4, { source: 'playground' });
    // has_output 分流状态标签
    expect(container.querySelector('[data-testid="playground-recent-row-f-done"]')?.textContent).toContain(
      '查看结果',
    );
    expect(container.querySelector('[data-testid="playground-recent-row-f-review"]')?.textContent).toContain(
      '继续复核',
    );
    // 点击 = 既有深链恢复协议（playground-page resume effect 承接）
    fireEvent.click(container.querySelector('[data-testid="playground-recent-row-f-done"]') as Element);
    expect(screen.getByTestId('location-probe').textContent).toContain('file_id=f-done');
  });

  it('拉取失败/为空 → 整块不渲染（宁缺勿谎）', async () => {
    mockFilesState.list.mockRejectedValue(new Error('network down'));
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = renderUpload(makeCtx(rec));
    // resolve 后仍无块
    await vi.waitFor(() => {
      expect(container.querySelector('[data-testid="playground-recent"]')).toBeNull();
    });
  });
});

describe('Issue#355 投料态 ds：大拖拽位（ds-only DOM）', () => {
  it('ds 渲染格式 chips 行 + 绿胶囊 CTA（选择文件）；dropzone 源码钉 ds 类钩子', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = renderUpload(makeCtx(rec));

    expect(container.querySelector('[data-testid="playground-format-chips"]')).not.toBeNull();
    // CTA 文案=「选择文件」（classic 口径「或点击选择文件」不混用）
    const cta = container.querySelector('.ds-drop-cta');
    expect(cta?.textContent).toContain('选择文件');
    expect(dropzoneSource).toContain('ds-drop-ico');
    expect(uploadSource).toContain('ds-upload-hero');
    expect(uploadSource).toContain('ds-cfg-strip');
  });

  it('ds 页头源码钉：playground-page 渲染 ds-page-title 页头 + 识别历史链接（挂 /jobs?view=done）', () => {
    expect(pageSource).toContain('ds-page-head');
    expect(pageSource).toContain("t('playground.upload.pageTitle')");
    expect(pageSource).toContain("t('playground.upload.historyLink')");
    expect(pageSource).toContain('/jobs?view=done');
  });
});

describe('Issue#355 投料态 classic 零新 DOM', () => {
  it('classic：无 config strip / recent / format chips（ds-only 块全部缺席）', () => {
    useUiTheme.setState({ theme: 'classic' });
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = renderUpload(makeCtx(rec));

    expect(container.querySelector('[data-testid="playground-config-strip"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-recent"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-format-chips"]')).toBeNull();
    expect(container.querySelector('[data-testid="playground-upload-hero"]')).toBeNull();
    // classic 摘要卡仍在（既有 DOM 零漂移）
    expect(container.querySelector('[data-testid="playground-type-panel"]')).not.toBeNull();
  });
});
