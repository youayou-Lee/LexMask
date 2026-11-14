// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { resolveLegalTextSelection } from '@/config/legalPreset';
import { useI18n } from '@/i18n';
import type { ServicesHealth } from '@/hooks/use-service-health';
import type { usePlayground } from '../hooks/use-playground';
import type { EntityTypeConfig } from '../types';
import pageSource from '../playground-page.tsx?raw';
import dropzoneSource from './playground-upload-dropzone.tsx?raw';
import { PlaygroundUpload } from './playground-upload';

/**
 * Issue#262 Task 9b：上传页重皮对齐界面稿（紧凑页头 / 上传大卡 / 摘要卡 / 告警人话化）组件钉。
 *
 * - 摘要卡：playground-type-panel 容器沿用，内含 chip 行（法律卷宗（默认）/ 全部类型）、
 *   「高级 · 按类型微调」入口（Task 10b 起开识别范围三区弹窗）、
 *   标签墙（selectedTypes 实时镜像，弹窗勾选同步）、说明行。
 * - 告警：playground-offline-hint 沿用——仅「视觉特征服务离线 且 当前选中含视觉要素」
 *   走人话主文案；黑话细节（./start_local.sh 等）收进「详情」折叠，默认不铺开。
 * - 页头紧凑化：playground-page.tsx 不再引用旧 kicker / 大标题键；dropzone 内装饰性
 *   格式 tabs 行（standards）删除（经核实为纯文案、无状态无回调，非功能选择器）。
 *
 * useServiceHealth / use-presets 经 vi.mock 注入（PlaygroundUpload 与三区弹窗内部都会调用，
 * 避免 jsdom 网络轮询/QueryClient 缺失）；rec mock 逐键对齐 use-playground-recognition
 * 返回形状（同 playground-upload.legal-preset.test.tsx 的 mock 约定）。
 */

const mockHealthState = vi.hoisted(() => ({
  health: null as ServicesHealth | null,
  checking: false,
}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({
    health: mockHealthState.health,
    checking: mockHealthState.checking,
    roundTripMs: null,
    refresh: vi.fn(),
  }),
}));

// Task 10b：高级入口改开识别范围三区弹窗（recognition-scope-dialog）——弹窗打开时
// 会跑 usePresets，按 redact-task 测试的稳定引用约定 mock，避免 jsdom 无 QueryClient。
vi.mock('@/services/hooks/use-presets', () => {
  const stablePresets: never[] = [];
  return {
    usePresets: () => ({ data: stablePresets, isLoading: false }),
    useInvalidatePresets: () => async () => {},
  };
});

type PlaygroundApi = ReturnType<typeof usePlayground>;
type RecognitionCtx = PlaygroundApi['recognition'];

// 目录：3 项后端默认启用（PERSON/PHONE/CASE_NUMBER）+ 2 项非默认（MED_PATIENT/BANK_CARD）。
// 法律默认选中项 = resolveLegalTextSelection(目录) = PERSON/PHONE/BANK_CARD/CASE_NUMBER。
const CATALOG_A: EntityTypeConfig[] = [
  { id: 'PERSON', name: 'Person', color: '#000000', enabled: true, default_enabled: true },
  { id: 'PHONE', name: 'Phone', color: '#000000', enabled: true, default_enabled: true },
  { id: 'CASE_NUMBER', name: 'CaseNumber', color: '#000000', enabled: true, default_enabled: true },
  { id: 'MED_PATIENT', name: 'Patient', color: '#000000', enabled: true, default_enabled: false },
  { id: 'BANK_CARD', name: 'BankCard', color: '#000000', enabled: true, default_enabled: false },
];

const LEGAL_IDS = resolveLegalTextSelection(CATALOG_A.map((type) => type.id));
const BACKEND_DEFAULT_IDS = CATALOG_A.filter((type) => type.default_enabled).map((type) => type.id);

function makeHealth(visualStatus: 'online' | 'offline'): ServicesHealth {
  return {
    all_online: visualStatus === 'online',
    services: {
      paddle_ocr: { name: 'PaddleOCR', status: 'online' },
      has_ner: { name: 'HaS Text', status: 'online' },
      visual_features: { name: 'visual', status: visualStatus },
    },
  };
}

function makeRec(overrides: {
  entityTypes: EntityTypeConfig[];
  selectedTypes: string[];
  selectedVisualFeatureTypes?: string[];
  playgroundPresetTextId?: string | null;
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
    selectedVisualFeatureTypes: overrides.selectedVisualFeatureTypes ?? [],
    selectedOcrHasTypesRef: { current: [] },
    selectedVisualFeatureTypesRef: { current: overrides.selectedVisualFeatureTypes ?? [] },
    pipelines: [],
    processingMode: 'mask',
    setProcessingMode: vi.fn(),
    replacementMode: 'mask',
    setReplacementMode: vi.fn(),
    watermarkText: '',
    setWatermarkText: vi.fn(),
    watermarkDelivery: 'keep',
    setWatermarkDelivery: vi.fn(),
    textPresetsPg: [],
    visionPresetsPg: [],
    playgroundPresetTextId: overrides.playgroundPresetTextId ?? null,
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

beforeEach(() => {
  // 测试内 t() 输出钉死 zh（en 是按需 code-split，异步加载会让文本断言漂移）。
  useI18n.setState({ locale: 'zh' });
  mockHealthState.health = null;
  mockHealthState.checking = false;
});

afterEach(() => {
  cleanup();
  mockHealthState.health = null;
  mockHealthState.checking = false;
});

describe('Task 9b：摘要卡（chip 行 / 标签墙 / 说明行 / 高级入口）', () => {
  it('摘要卡容器+chip 行+高级入口+说明行存在；标签墙初始=法律默认选中项', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);

    expect(container.querySelector('[data-testid="playground-type-panel"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="playground-preset-chip-legal"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="playground-preset-chip-all"]')).not.toBeNull();
    const entry = container.querySelector('[data-testid="playground-scope-entry"]');
    expect(entry).not.toBeNull();
    expect(entry?.getAttribute('aria-haspopup')).toBe('dialog');
    expect(entry?.getAttribute('aria-expanded')).toBe('false');
    // 标签墙初始 = 法律默认 4 类（后端默认集之外的 BANK_CARD 在法律集内、MED_PATIENT 不在）
    expect(container.querySelector('[data-testid="playground-selected-tags"]')).not.toBeNull();
    for (const id of ['PERSON', 'PHONE', 'BANK_CARD', 'CASE_NUMBER']) {
      expect(container.querySelector(`[data-testid="playground-selected-tag-${id}"]`)).not.toBeNull();
    }
    expect(
      container.querySelector('[data-testid="playground-selected-tag-MED_PATIENT"]'),
    ).toBeNull();
    // 标签墙文案 = 选中项数，不多不少
    expect(container.querySelectorAll('[data-testid="playground-selected-tags"] > span')).toHaveLength(
      LEGAL_IDS.length,
    );
    // 说明行（预览口径）
    expect(container.textContent).toContain('80+');
    expect(container.textContent).toContain('高级 · 按类型微调');
  });

  it('标签墙实时镜像 selectedTypes（抽屉勾选同步到标签墙）', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const ctx = makeCtx(rec);
    const { container, rerender } = render(<PlaygroundUpload ctx={ctx} />);
    expect(container.querySelector('[data-testid="playground-selected-tag-BANK_CARD"]')).not.toBeNull();

    // 用户在抽屉里勾了 MED_PATIENT、取消 BANK_CARD（选择引用换新实例）
    rec.selectedTypes = ['PERSON', 'MED_PATIENT'];
    rerender(<PlaygroundUpload ctx={ctx} />);
    expect(container.querySelector('[data-testid="playground-selected-tag-MED_PATIENT"]')).not.toBeNull();
    expect(container.querySelector('[data-testid="playground-selected-tag-BANK_CARD"]')).toBeNull();
    expect(container.querySelectorAll('[data-testid="playground-selected-tags"] > span')).toHaveLength(2);
  });

  it('高级入口点击打开识别范围三区弹窗（Task 10b：T6 抽屉已替换为弹窗）', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    const entry = container.querySelector('[data-testid="playground-scope-entry"]');
    expect(entry).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-dialog')).toBeNull();
    fireEvent.click(entry as Element);
    expect(entry?.getAttribute('aria-expanded')).toBe('true');
    // Radix Dialog 渲染进 portal（document.body），用 screen 查询
    expect(screen.queryByTestId('playground-scope-dialog')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-zone-ner')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-zone-ocr')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-zone-la')).not.toBeNull();
  });
});

describe('Task 9b：告警人话化（playground-offline-hint 沿用）', () => {
  it('视觉特征离线 + 选中含视觉要素 → 告警出现，默认文案人话（无 start_local），黑话在「详情」折叠里', () => {
    mockHealthState.health = makeHealth('offline');
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...LEGAL_IDS],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL', 'SIGNATURE'],
    });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);

    const hint = container.querySelector('[data-testid="playground-offline-hint"]');
    expect(hint).not.toBeNull();
    expect(hint?.textContent).toContain('视觉特征服务离线');
    expect(hint?.textContent).toContain('暂不识别');
    // 人话口径：主文案不出现启动命令等黑话
    expect(hint?.textContent).not.toContain('start_local');
    // 黑话细节收进「详情」折叠，默认收起
    const detailsButton = container.querySelector('[data-testid="playground-offline-hint-details"]');
    expect(detailsButton).not.toBeNull();
    expect(detailsButton?.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(detailsButton as Element);
    expect(detailsButton?.getAttribute('aria-expanded')).toBe('true');
    // 展开后黑话细节与受影响服务可见（信息不丢失，只是不默认铺开）
    expect(hint?.textContent).toContain('start_local');
    expect(container.querySelector('[data-testid="playground-affected-services"]')).not.toBeNull();
  });

  it('视觉特征离线 + 当前选中不含视觉要素 → 不出现告警（不打扰）', () => {
    mockHealthState.health = makeHealth('offline');
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    expect(container.querySelector('[data-testid="playground-offline-hint"]')).toBeNull();
  });

  // Issue#270：组合故障（视觉 + 其他服务同时 blocked）主文案要涵盖两者——
  // 不再复用单视觉文案（其结尾「其余识别不受影响」在组合故障下是错误陈述）。
  it('组合故障（视觉+其他服务同时 blocked，已选视觉要素）→ 主文案为组合版，不含「其余识别不受影响」', () => {
    mockHealthState.health = {
      all_online: false,
      services: {
        paddle_ocr: { name: 'PaddleOCR', status: 'offline' },
        has_ner: { name: 'HaS Text', status: 'online' },
        visual_features: { name: 'visual', status: 'offline' },
      },
    };
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...LEGAL_IDS],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL', 'SIGNATURE'],
    });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);

    const hint = container.querySelector('[data-testid="playground-offline-hint"]');
    expect(hint).not.toBeNull();
    // 组合版主文案特征片段（visualOfflineCombinedHint）
    expect(hint?.textContent).toContain('均未正常运行');
    // 不含单视觉文案的错误陈述（旧代码渲染 visualOfflineHint 必含 → 对旧代码红）
    expect(hint?.textContent).not.toContain('其余识别不受影响');
    // 人话口径不变：主文案无黑话；详情折叠仍如实列出全部受影响服务
    expect(hint?.textContent).not.toContain('start_local');
    fireEvent.click(container.querySelector('[data-testid="playground-offline-hint-details"]') as Element);
    expect(hint?.textContent).toContain('start_local');
    expect(container.querySelector('[data-testid="playground-affected-services"]')).not.toBeNull();
  });

  it('视觉特征离线但其余服务离线（视觉未选）→ 走通用提示（paddle_ocr 离线仍要知情）', () => {
    mockHealthState.health = {
      all_online: false,
      services: {
        paddle_ocr: { name: 'PaddleOCR', status: 'offline' },
        has_ner: { name: 'HaS Text', status: 'online' },
        visual_features: { name: 'visual', status: 'offline' },
      },
    };
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    const hint = container.querySelector('[data-testid="playground-offline-hint"]');
    expect(hint).not.toBeNull();
    expect(hint?.textContent).not.toContain('start_local');
  });

  it('服务全在线 → 不出现告警', () => {
    mockHealthState.health = makeHealth('online');
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...LEGAL_IDS],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
    });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    expect(container.querySelector('[data-testid="playground-offline-hint"]')).toBeNull();
  });

  it('后端不可达 → 上传禁用 + 提示保留（黑话在折叠里不默认铺开）', () => {
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    const dropzone = container.querySelector('[data-testid="playground-dropzone"]');
    expect(dropzone?.getAttribute('aria-disabled')).toBe('true');
    const hint = container.querySelector('[data-testid="playground-offline-hint"]');
    expect(hint).not.toBeNull();
    expect(hint?.textContent).not.toContain('start_local');
    fireEvent.click(container.querySelector('[data-testid="playground-offline-hint-details"]') as Element);
    expect(hint?.textContent).toContain('start_local');
  });

  it('告警「详情」折叠 aria-controls 指向面板 id 且详情面板长在告警卡内（动态读值 + 身份断言）', () => {
    // Issue#266 ③：aria-controls 动态读值防配对断裂（M4）；身份断言（M4b）
    // 锁「详情面板是告警卡的后代」防配错容器；收起态条件挂载不在 DOM。
    const rec = makeRec({ entityTypes: CATALOG_A, selectedTypes: [...LEGAL_IDS] });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    const details = container.querySelector(
      '[data-testid="playground-offline-hint-details"]',
    ) as Element;
    expect(details).not.toBeNull();
    const controlsId = details.getAttribute('aria-controls');
    expect(controlsId).not.toBeNull();
    expect(document.getElementById(controlsId as string)).toBeNull();
    fireEvent.click(details);
    const panel = document.getElementById(controlsId as string);
    expect(panel).not.toBeNull();
    const hint = container.querySelector('[data-testid="playground-offline-hint"]');
    expect(hint?.contains(panel as Node)).toBe(true);
  });
});

describe('Task 9b：紧凑页头源码钉（旧 kicker / 装饰性格式行移除）', () => {
  it('playground-page.tsx 页头不再引用旧 kicker / 大标题 / 长说明键，改用紧凑页头键', () => {
    expect(pageSource).not.toContain("t('playground.upload.kicker')");
    expect(pageSource).not.toContain("t('playground.title')");
    expect(pageSource).not.toContain("t('page.playground.sub')");
    expect(pageSource).toContain("t('playground.upload.pageTitle')");
    expect(pageSource).toContain("t('playground.upload.pageSub')");
  });

  it('上传卡内不再有装饰性格式 tabs 行（拖入提示 / 支持格式保留）', () => {
    expect(dropzoneSource).not.toContain('playground.upload.kicker');
    expect(dropzoneSource).not.toContain('playground.upload.title');
    expect(dropzoneSource).not.toContain('playground.upload.desc');
    expect(dropzoneSource).not.toContain('playground.upload.standards');
    expect(dropzoneSource).not.toContain('splitStandards');
    expect(dropzoneSource).toContain("t('playground.dropHere')");
    expect(dropzoneSource).toContain("t('playground.supportedFormats')");
  });
});

// 防御性钉：上述用例依赖「initial selectedTypes ≠ 后端默认集」以跳过法律默认挂载 effect，
// 若目录/默认集漂移导致 effect 触发，用 mock setSelectedTypes 的 no-op 特性不影响 DOM 断言，
// 但这里仍显式钉住两集不相等，避免测试前提静默失效。
describe('Task 9b：测试前提自检', () => {
  it('LEGAL_IDS 与后端默认集不相等（标签墙初始断言的前提）', () => {
    expect(LEGAL_IDS).not.toEqual(BACKEND_DEFAULT_IDS);
    expect(LEGAL_IDS).toEqual(['PERSON', 'PHONE', 'BANK_CARD', 'CASE_NUMBER']);
  });
});
