// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { useI18n } from '@/i18n';
import { showToast } from '@/components/Toast';
import { resolveLegalTextSelection } from '@/config/legalPreset';
import { buildDefaultPipelineTypeIds } from '@/services/defaultRedactionPreset';
import type { ServicesHealth } from '@/hooks/use-service-health';
import type { RecognitionPreset } from '@/services/presetsApi';
import type { usePlayground } from '../hooks/use-playground';
import type { EntityTypeConfig, PipelineConfig, VisionTypeConfig } from '../types';
import { RecognitionScopeDialog } from './recognition-scope-dialog';

/**
 * Issue#262 二期 Task 10b：识别范围三区弹窗组件钉。
 *
 * - 三区（ner=语义识别/ocr=文字有无检测/la=视觉特征定位）+ 服务徽标 + 分页 chip 墙 +
 *   区内全选/全不选 + 服务离线暗区 + 纯文本不适用 + 即点即存 + 方案三态 + 保存为方案。
 * - useServiceHealth / use-presets / presetsApi.createPreset / Toast 经 vi.mock 注入；
 *   rec mock 逐键对齐 use-playground-recognition.ts 返回形状（同 reskin/legal-preset 测试约定）。
 * - Radix Dialog 渲染进 portal（document.body），断言一律走 screen 查询。
 */

const mockHealthState = vi.hoisted(() => ({
  health: null as ServicesHealth | null,
  checking: false,
}));

const mockPresetState = vi.hoisted(() => ({
  list: [] as RecognitionPreset[],
}));

const createPresetMock = vi.hoisted(() => vi.fn());
const invalidatePresetsMock = vi.hoisted(() => vi.fn(async () => {}));

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({
    health: mockHealthState.health,
    checking: mockHealthState.checking,
    roundTripMs: null,
    refresh: vi.fn(),
  }),
}));

vi.mock('@/services/hooks/use-presets', () => ({
  // 稳定引用约定（同 use-playground.redact-task.test.tsx）：避免 data 每渲染新数组触发副作用抖动
  usePresets: () => ({ data: mockPresetState.list, isLoading: false }),
  useInvalidatePresets: () => invalidatePresetsMock,
}));

vi.mock('@/services/presetsApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/presetsApi')>();
  return { ...actual, createPreset: createPresetMock };
});

vi.mock('@/components/Toast', () => ({ showToast: vi.fn() }));

type PlaygroundApi = ReturnType<typeof usePlayground>;
type RecognitionCtx = PlaygroundApi['recognition'];

// 文本目录：默认集 3 项 + 非默认 2 项（法律集 = PERSON/PHONE/BANK_CARD/CASE_NUMBER）。
const TEXT_CATALOG: EntityTypeConfig[] = [
  { id: 'PERSON', name: 'Person', color: '#000000', enabled: true, default_enabled: true },
  { id: 'PHONE', name: 'Phone', color: '#000000', enabled: true, default_enabled: true },
  { id: 'CASE_NUMBER', name: 'CaseNumber', color: '#000000', enabled: true, default_enabled: true },
  { id: 'MED_PATIENT', name: 'Patient', color: '#000000', enabled: true, default_enabled: false },
  { id: 'BANK_CARD', name: 'BankCard', color: '#000000', enabled: true, default_enabled: false },
];

// 分页用 20 类文本目录（T01..T20，全部 default_enabled=false 以免搅动法律集断言）。
const WIDE_CATALOG: EntityTypeConfig[] = Array.from({ length: 20 }, (_, i) => ({
  id: `T${String(i + 1).padStart(2, '0')}`,
  name: `Type${i + 1}`,
  color: '#000000',
  enabled: true,
  default_enabled: false,
}));

// 管线目录：default_enabled 标记后端默认集（VisionTypeConfig 类型面无此字段——
// buildDefaultPipelineTypeIds 按 DefaultPipelineTypeLike 结构读取，这里局部补型）。
type TestPipeline = Omit<PipelineConfig, 'types'> & {
  types: (VisionTypeConfig & { default_enabled?: boolean })[];
};

const PIPELINES: TestPipeline[] = [
  {
    mode: 'ocr_has',
    name: 'ocr',
    description: '',
    enabled: true,
    types: [
      { id: 'OCR_ID', name: 'ocr id', color: '#000000', default_enabled: true },
      { id: 'OCR_PHONE', name: 'ocr phone', color: '#000000', default_enabled: true },
      { id: 'OCR_ADDR', name: 'ocr addr', color: '#000000', default_enabled: false },
    ],
  },
  {
    mode: 'visual_features',
    name: 'visual',
    description: '',
    enabled: true,
    types: [
      { id: 'OFFICIAL_SEAL', name: '印章', color: '#000000', default_enabled: true },
      { id: 'SIGNATURE', name: '签名', color: '#000000', default_enabled: true },
      { id: 'HANDWRITING', name: '手写批注', color: '#000000', default_enabled: false },
    ],
  },
];

const OCR_IDS = PIPELINES[0].types.map((type) => type.id);
const VISUAL_IDS = PIPELINES[1].types.map((type) => type.id);
const OCR_DEFAULT_IDS = buildDefaultPipelineTypeIds(PIPELINES, 'ocr_has');
const VISUAL_DEFAULT_IDS = buildDefaultPipelineTypeIds(PIPELINES, 'visual_features');
const LEGAL_IDS = resolveLegalTextSelection(TEXT_CATALOG.map((type) => type.id));

const USER_PRESET: RecognitionPreset = {
  id: 'preset-1',
  name: '我的方案A',
  kind: 'full',
  selectedEntityTypeIds: ['PERSON'],
  ocrHasTypes: ['OCR_ID'],
  visualFeatureTypes: ['OFFICIAL_SEAL'],
  replacementMode: 'structured',
  created_at: '2026-01-01T00:00:00Z',
  updated_at: '2026-01-01T00:00:00Z',
};

interface RecOverrides {
  entityTypes?: EntityTypeConfig[];
  pipelines?: TestPipeline[];
  selectedTypes?: string[];
  selectedOcrHasTypes?: string[];
  selectedVisualFeatureTypes?: string[];
  playgroundPresetTextId?: string | null;
  playgroundPresetVisionId?: string | null;
  textPresetsPg?: RecognitionPreset[];
  visionPresetsPg?: RecognitionPreset[];
  replacementMode?: 'structured' | 'smart' | 'mask' | 'pseudonym';
}

function makeRec(overrides: RecOverrides = {}): RecognitionCtx {
  const entityTypes = overrides.entityTypes ?? TEXT_CATALOG;
  const pipelines = overrides.pipelines ?? PIPELINES;
  return {
    entityTypes,
    textConfigState: 'ready',
    selectedTypes: overrides.selectedTypes ?? [...LEGAL_IDS],
    selectedTypesRef: { current: overrides.selectedTypes ?? [...LEGAL_IDS] },
    setSelectedTypes: vi.fn(),
    visionTypes: pipelines.flatMap((pipeline) => pipeline.types),
    visionConfigState: 'ready',
    selectedOcrHasTypes: overrides.selectedOcrHasTypes ?? [...OCR_DEFAULT_IDS],
    selectedVisualFeatureTypes: overrides.selectedVisualFeatureTypes ?? [...VISUAL_DEFAULT_IDS],
    selectedOcrHasTypesRef: { current: overrides.selectedOcrHasTypes ?? [...OCR_DEFAULT_IDS] },
    selectedVisualFeatureTypesRef: {
      current: overrides.selectedVisualFeatureTypes ?? [...VISUAL_DEFAULT_IDS],
    },
    pipelines,
    typeTab: 'text',
    setTypeTab: vi.fn(),
    processingMode: 'mask',
    setProcessingMode: vi.fn(),
    replacementMode: overrides.replacementMode ?? 'structured',
    setReplacementMode: vi.fn(),
    watermarkText: '',
    setWatermarkText: vi.fn(),
    watermarkDelivery: 'keep',
    setWatermarkDelivery: vi.fn(),
    textPresetsPg: overrides.textPresetsPg ?? [],
    visionPresetsPg: overrides.visionPresetsPg ?? [],
    playgroundPresetTextId: overrides.playgroundPresetTextId ?? null,
    playgroundPresetVisionId: overrides.playgroundPresetVisionId ?? null,
    selectPlaygroundTextPresetById: vi.fn(),
    selectPlaygroundVisionPresetById: vi.fn(),
    saveTextPresetFromPlayground: vi.fn(),
    saveVisionPresetFromPlayground: vi.fn(),
    presetDialogKind: null,
    presetDialogName: '',
    setPresetDialogName: vi.fn(),
    presetSaving: false,
    closePresetDialog: vi.fn(),
    openTextPresetDialog: vi.fn(),
    openVisionPresetDialog: vi.fn(),
    clearPlaygroundTextPresetTracking: vi.fn(),
    clearPlaygroundVisionPresetTracking: vi.fn(),
    sortedEntityTypes: entityTypes,
    playgroundTextGroups: [],
    setPlaygroundTextTypeGroupSelection: vi.fn(),
    toggleVisionType: vi.fn(),
    updateOcrHasTypes: vi.fn(),
    updateVisualFeatureTypes: vi.fn(),
    presetApplySeq: 0,
    getTypeConfig: vi.fn(),
    getVisionTypeConfig: vi.fn(),
  } as unknown as RecognitionCtx;
}

function makeHealth(
  statuses: Partial<Record<'paddle_ocr' | 'has_ner' | 'visual_features', ServicesHealth['services']['paddle_ocr']['status']>>,
): ServicesHealth {
  return {
    all_online: false,
    services: {
      paddle_ocr: { name: 'PaddleOCR', status: statuses.paddle_ocr ?? 'online' },
      has_ner: { name: 'HaS Text', status: statuses.has_ner ?? 'online' },
      visual_features: { name: 'visual', status: statuses.visual_features ?? 'online' },
    },
  };
}

function renderDialog(rec: RecognitionCtx, props: { visionApplicable?: boolean } = {}) {
  const onOpenChange = vi.fn();
  render(
    <RecognitionScopeDialog
      open
      onOpenChange={onOpenChange}
      rec={rec}
      visionApplicable={props.visionApplicable ?? true}
    />,
  );
  return { onOpenChange, rec };
}

const zone = (id: 'ner' | 'ocr' | 'la') => screen.getByTestId(`playground-scope-zone-${id}`);
const chip = (typeId: string) =>
  screen.getByTestId(`playground-scope-chip-${typeId}`) as HTMLButtonElement;
const zoneAll = (id: 'ner' | 'ocr' | 'la') =>
  screen.getByTestId(`playground-scope-zone-${id}-all`) as HTMLButtonElement;
const zoneNone = (id: 'ner' | 'ocr' | 'la') =>
  screen.getByTestId(`playground-scope-zone-${id}-none`) as HTMLButtonElement;

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
  mockHealthState.health = makeHealth({});
  mockHealthState.checking = false;
  mockPresetState.list = [USER_PRESET];
  createPresetMock.mockReset();
  invalidatePresetsMock.mockClear();
  vi.mocked(showToast).mockClear();
});

afterEach(() => {
  cleanup();
  mockHealthState.health = null;
  mockPresetState.list = [];
});

describe('Task 10b：三区渲染与服务徽标', () => {
  it('三区 + 区名 + 服务徽标 + 角色一句话 + 方案行 + 页脚计数与说明存在', () => {
    const rec = makeRec();
    renderDialog(rec);

    expect(screen.getByTestId('playground-scope-dialog')).not.toBeNull();
    expect(zone('ner').textContent).toContain('语义识别');
    expect(zone('ocr').textContent).toContain('文字有无检测');
    expect(zone('la').textContent).toContain('视觉特征定位');
    // 服务徽标 = 服务名 + 状态（在线）
    expect(zone('ner').textContent).toContain('文本语义服务');
    expect(zone('ocr').textContent).toContain('文字识别服务');
    expect(zone('la').textContent).toContain('视觉特征服务');
    expect(zone('la').textContent).toContain('在线');
    // 方案行：label + select + 保存为方案
    expect(screen.getByTestId('playground-scheme-select')).not.toBeNull();
    expect(screen.getByTestId('playground-scheme-save')).not.toBeNull();
    // 页脚：已选计数（法律 4 + ocr 默认 2 + 视觉默认 2 = 8）+ 无需保存说明
    expect(screen.getByTestId('playground-scope-selected-count').textContent).toContain('8');
    expect(screen.getByTestId('playground-scope-footer-note').textContent).toContain('无需保存');
  });

  it('分页：20 类 NER 区 → 第1/3页·共20类；下一页看 9-16；上一页回 1-8', () => {
    const rec = makeRec({
      entityTypes: WIDE_CATALOG,
      pipelines: [],
      selectedTypes: [],
      selectedOcrHasTypes: [],
      selectedVisualFeatureTypes: [],
    });
    renderDialog(rec);

    expect(zone('ner').textContent).toContain('第1/3页');
    expect(zone('ner').textContent).toContain('共20类');
    expect(chip('T01')).not.toBeNull();
    expect(chip('T08')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-chip-T09')).toBeNull();

    fireEvent.click(screen.getByTestId('playground-scope-zone-ner-page-next'));
    expect(zone('ner').textContent).toContain('第2/3页');
    expect(chip('T09')).not.toBeNull();
    expect(chip('T16')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-chip-T01')).toBeNull();

    fireEvent.click(screen.getByTestId('playground-scope-zone-ner-page-prev'));
    expect(zone('ner').textContent).toContain('第1/3页');
    expect(chip('T01')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-chip-T09')).toBeNull();
  });
});

describe('Task 10b：区内全选/全不选写 rec', () => {
  it('NER 全选 → setSelectedTypes(全部 id)+clearTextTracking；全不选 → setSelectedTypes([])', () => {
    const rec = makeRec({
      entityTypes: WIDE_CATALOG,
      pipelines: [],
      selectedTypes: [],
      selectedOcrHasTypes: [],
      selectedVisualFeatureTypes: [],
    });
    renderDialog(rec);

    fireEvent.click(zoneAll('ner'));
    expect(rec.setSelectedTypes).toHaveBeenCalledWith(WIDE_CATALOG.map((type) => type.id));
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();

    fireEvent.click(zoneNone('ner'));
    expect(rec.setSelectedTypes).toHaveBeenCalledWith([]);
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();
  });

  it('LA 全选 → clearVisionTracking+updateVisualFeatureTypes(全部)；OCR 全不选 → updateOcrHasTypes([])', () => {
    const rec = makeRec();
    renderDialog(rec);

    fireEvent.click(zoneAll('la'));
    expect(rec.clearPlaygroundVisionPresetTracking).toHaveBeenCalled();
    expect(rec.updateVisualFeatureTypes).toHaveBeenCalledWith(VISUAL_IDS);

    fireEvent.click(zoneNone('ocr'));
    expect(rec.updateOcrHasTypes).toHaveBeenCalledWith([]);
    expect(rec.clearPlaygroundVisionPresetTracking).toHaveBeenCalled();
  });
});

describe('Task 10b：即点即存（点 chip → setter 调用）', () => {
  it('NER chip 勾/取消 → clearTextTracking+setSelectedTypes(函数式更新器)；OCR/LA chip → toggleVisionType(id, 模式)', () => {
    const rec = makeRec({ selectedTypes: ['PERSON', 'PHONE'] });
    renderDialog(rec);

    // 取消 PERSON（当前已选）——函数式更新器：执行后语义可验
    fireEvent.click(chip('PERSON'));
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    const updater = vi.mocked(rec.setSelectedTypes).mock.calls[0][0] as (
      previous: string[],
    ) => string[];
    expect(updater(['PERSON', 'PHONE'])).toEqual(['PHONE']);
    expect(updater(['PERSON'])).toEqual([]);

    // 勾上 BANK_CARD（当前未选）
    fireEvent.click(chip('BANK_CARD'));
    const adder = vi.mocked(rec.setSelectedTypes).mock.calls[1][0] as (
      previous: string[],
    ) => string[];
    expect(adder(['PERSON', 'PHONE'])).toEqual(['PERSON', 'PHONE', 'BANK_CARD']);

    // OCR / LA chip → toggleVisionType
    fireEvent.click(chip('OCR_ADDR'));
    expect(rec.toggleVisionType).toHaveBeenCalledWith('OCR_ADDR', 'ocr_has');
    fireEvent.click(chip('HANDWRITING'));
    expect(rec.toggleVisionType).toHaveBeenCalledWith('HANDWRITING', 'visual_features');
  });
});

describe('Task 10b：服务离线 / 降级 / 检测', () => {
  it('LA offline → 红色徽标 + 区内红条 + chips/页脚按钮 disabled + 页脚计数不含 LA', () => {
    mockHealthState.health = makeHealth({ visual_features: 'offline' });
    const rec = makeRec({
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL', 'SIGNATURE'],
    });
    renderDialog(rec);

    const la = zone('la');
    expect(la.className).toContain('grayscale');
    expect(la.textContent).toContain('本区暂不参与识别，恢复后自动回来');
    expect(la.textContent).toContain('离线');
    expect(zoneAll('la').disabled).toBe(true);
    expect(zoneNone('la').disabled).toBe(true);
    // 其他区不受影响
    expect(chip('PERSON').disabled).toBe(false);
    // 页脚计数 = NER 4 + OCR 2（LA 2 类被排除）
    expect(screen.getByTestId('playground-scope-selected-count').textContent).toContain('6');
    // LA 区不渲染 chip 墙（离线替代内容）
    expect(screen.queryByTestId('playground-scope-chip-OFFICIAL_SEAL')).toBeNull();
  });

  it('busy / checking 视为可用（chips enabled、无红条）；degraded 不算离线（徽标显示降级）', () => {
    const rec = makeRec();
    const { rerender } = render(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />,
    );
    expect(chip('OFFICIAL_SEAL').disabled).toBe(false);

    mockHealthState.health = makeHealth({ visual_features: 'busy' });
    rerender(<RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />);
    expect(chip('OFFICIAL_SEAL').disabled).toBe(false);
    expect(zone('la').textContent).not.toContain('本区暂不参与识别');

    mockHealthState.health = makeHealth({ visual_features: 'checking' });
    rerender(<RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />);
    expect(chip('OFFICIAL_SEAL').disabled).toBe(false);

    mockHealthState.health = makeHealth({ visual_features: 'degraded' });
    rerender(<RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />);
    expect(chip('OFFICIAL_SEAL').disabled).toBe(false);
    expect(zone('la').textContent).toContain('降级');
    expect(zone('la').textContent).not.toContain('本区暂不参与识别');
  });

  it('后端不可达（health=null 且 checking=false）→ 三区全部按离线暗化', () => {
    mockHealthState.health = null;
    mockHealthState.checking = false;
    const rec = makeRec();
    renderDialog(rec);

    for (const id of ['ner', 'ocr', 'la'] as const) {
      expect(zone(id).className).toContain('grayscale');
      expect(zone(id).textContent).toContain('本区暂不参与识别');
      expect(zoneAll(id).disabled).toBe(true);
      expect(zoneNone(id).disabled).toBe(true);
    }
    // 离线区不渲染 chip 墙
    expect(screen.queryByTestId('playground-scope-chip-PERSON')).toBeNull();
  });

  it('纯文本 + LA 离线（组合态）→ na 优先：OCR/LA 区显示 na 文案、不暗区、无离线红条；服务徽标仍如实显示离线', () => {
    mockHealthState.health = makeHealth({ visual_features: 'offline' });
    const rec = makeRec({
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
    });
    renderDialog(rec, { visionApplicable: false });

    for (const id of ['ocr', 'la'] as const) {
      expect(zone(id).textContent).toContain('本文件是纯文本，不走此区');
      expect(zone(id).textContent).not.toContain('本区暂不参与识别');
      expect(zone(id).className).not.toContain('grayscale');
      expect(zoneAll(id).disabled).toBe(true);
    }
    // 服务徽标如实显示离线（信息不丢）
    expect(zone('la').textContent).toContain('离线');
    // NER 区照常可用，页脚计数只含 NER
    expect(chip('PERSON').disabled).toBe(false);
    expect(screen.getByTestId('playground-scope-selected-count').textContent).toContain('4');
  });
});

describe('Task 10b：纯文本不适用（na 态 ≠ 离线）', () => {
  it('visionApplicable=false → OCR/LA 区显示 na 文案、无 chips、页脚按钮 disabled；计数只含 NER', () => {
    const rec = makeRec({
      selectedOcrHasTypes: ['OCR_ID', 'OCR_PHONE'],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
    });
    renderDialog(rec, { visionApplicable: false });

    expect(zone('ocr').textContent).toContain('本文件是纯文本，不走此区');
    expect(zone('la').textContent).toContain('本文件是纯文本，不走此区');
    expect(zone('ocr').textContent).not.toContain('本区暂不参与识别');
    expect(screen.queryByTestId('playground-scope-chip-OCR_ID')).toBeNull();
    expect(screen.queryByTestId('playground-scope-chip-OFFICIAL_SEAL')).toBeNull();
    expect(zoneAll('ocr').disabled).toBe(true);
    expect(zoneAll('la').disabled).toBe(true);
    // na 态服务在线 → 区不暗化（不是离线）
    expect(zone('ocr').className).not.toContain('grayscale');
    // 计数只含 NER 4 类（OCR 2 + LA 1 不参与）
    expect(screen.getByTestId('playground-scope-selected-count').textContent).toContain('4');
    // NER 区照常可用
    expect(chip('PERSON').disabled).toBe(false);
  });
});

describe('Task 10b：方案三态与自定义态', () => {
  it('legal 态：法律集 + 视觉默认 → select value=legal', () => {
    const rec = makeRec();
    renderDialog(rec);
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe('legal');
  });

  it('all 态：三清单全量 → select value=all', () => {
    const rec = makeRec({
      selectedTypes: TEXT_CATALOG.map((type) => type.id),
      selectedOcrHasTypes: OCR_IDS,
      selectedVisualFeatureTypes: VISUAL_IDS,
    });
    renderDialog(rec);
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe('all');
  });

  it('user 态：两维 tracking 一致指向 preset-1 且三清单与方案吻合 → select value=preset-1', () => {
    const rec = makeRec({
      selectedTypes: ['PERSON'],
      selectedOcrHasTypes: ['OCR_ID'],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
      playgroundPresetTextId: 'preset-1',
      playgroundPresetVisionId: 'preset-1',
    });
    renderDialog(rec);
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe(
      'preset-1',
    );
  });

  it('跨维 tracking 不一致（保存→套用→手动改 NER chip：text 维清空、vision 维仍指方案）→ 下拉进自定义态', () => {
    // 自动套用后的现场：三清单 = 方案A 清单，两维 tracking 都指 preset-1
    const rec = makeRec({
      selectedTypes: ['PERSON'],
      selectedOcrHasTypes: ['OCR_ID'],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
      playgroundPresetTextId: 'preset-1',
      playgroundPresetVisionId: 'preset-1',
    });
    const ctx = { rec };
    const { rerender } = render(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />,
    );
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe(
      'preset-1',
    );

    // 用户点一颗 NER chip（真 hook：setSelectedTypes 函数式生效 + 只清 text 维 tracking）
    fireEvent.click(chip('PHONE'));
    ctx.rec.selectedTypes = ['PERSON', 'PHONE'];
    ctx.rec.playgroundPresetTextId = null;
    rerender(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={ctx.rec} visionApplicable />,
    );
    // 旧实现 null ?? visionId 仍解析出方案名 → 失真；修复后跨维不一致 → 自定义态
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe(
      'custom',
    );
  });

  it('两维 tracking 一致但当前三清单已偏离方案 → 不显示方案名（状态一致性校验）', () => {
    const rec = makeRec({
      selectedTypes: ['PERSON', 'PHONE'], // 文本侧已偏离 preset-1 的 ['PERSON']
      selectedOcrHasTypes: ['OCR_ID'],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
      playgroundPresetTextId: 'preset-1',
      playgroundPresetVisionId: 'preset-1',
    });
    renderDialog(rec);
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe(
      'custom',
    );
  });

  it('自定义态：手动改选偏离两预置态且无 tracking → select value=custom', () => {
    const rec = makeRec({ selectedTypes: ['PERSON', 'MED_PATIENT'] });
    renderDialog(rec);
    const select = screen.getByTestId('playground-scheme-select') as HTMLSelectElement;
    expect(select.value).toBe('custom');
    // custom 选项存在但不可选（展示用）
    const customOption = select.querySelector('option[value="custom"]') as HTMLOptionElement;
    expect(customOption).not.toBeNull();
    expect(customOption.disabled).toBe(true);
  });

  // Issue#270 回归钉：方案被别处删除（presets 清单 refetch 不含所选方案）→
  // trackedPresetId 守卫（userPresets.find）返回 null → select 立即落自定义态，
  // value 恒在 options 内、无悬空空白一格。守卫被删（变异 M1）本钉必红。
  it('方案被删除（presets 清单刷新不含所选方案）→ select 立即落自定义（手动勾选），无悬空 value', () => {
    const rec = makeRec({
      selectedTypes: ['PERSON'],
      selectedOcrHasTypes: ['OCR_ID'],
      selectedVisualFeatureTypes: ['OFFICIAL_SEAL'],
      playgroundPresetTextId: 'preset-1',
      playgroundPresetVisionId: 'preset-1',
      textPresetsPg: [USER_PRESET],
      visionPresetsPg: [USER_PRESET],
    });
    const { rerender } = render(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />,
    );
    expect((screen.getByTestId('playground-scheme-select') as HTMLSelectElement).value).toBe(
      'preset-1',
    );

    // 模拟别处删除方案：presets 清单刷新后不再含 preset-1（rerender 同一新 rec）
    mockPresetState.list = [];
    rerender(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />,
    );
    const select = screen.getByTestId('playground-scheme-select') as HTMLSelectElement;
    expect(select.value).toBe('custom');
    // 渲染兜底确认：选中 option 是「自定义（手动勾选）」，不是空白一格
    expect(select.selectedOptions).toHaveLength(1);
    expect(select.selectedOptions[0].textContent).toBe('自定义（手动勾选）');
  });

  it('应用 legal → setSelectedTypes(法律集)+clearTextTracking+updateOcr/Visual(后端默认)+clearVisionTracking', () => {
    const rec = makeRec({
      selectedTypes: ['PERSON', 'MED_PATIENT'],
      selectedOcrHasTypes: OCR_IDS,
      selectedVisualFeatureTypes: VISUAL_IDS,
    });
    renderDialog(rec);

    fireEvent.change(screen.getByTestId('playground-scheme-select'), {
      target: { value: 'legal' },
    });
    expect(rec.setSelectedTypes).toHaveBeenCalledWith(LEGAL_IDS);
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();
    expect(rec.updateOcrHasTypes).toHaveBeenCalledWith(OCR_DEFAULT_IDS);
    expect(rec.updateVisualFeatureTypes).toHaveBeenCalledWith(VISUAL_DEFAULT_IDS);
    expect(rec.clearPlaygroundVisionPresetTracking).toHaveBeenCalled();
  });

  it('应用 all → setSelectedTypes(全部)+updateOcr/Visual(全量)', () => {
    const rec = makeRec();
    renderDialog(rec);

    fireEvent.change(screen.getByTestId('playground-scheme-select'), {
      target: { value: 'all' },
    });
    expect(rec.setSelectedTypes).toHaveBeenCalledWith(TEXT_CATALOG.map((type) => type.id));
    expect(rec.updateOcrHasTypes).toHaveBeenCalledWith(OCR_IDS);
    expect(rec.updateVisualFeatureTypes).toHaveBeenCalledWith(VISUAL_IDS);
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();
    expect(rec.clearPlaygroundVisionPresetTracking).toHaveBeenCalled();
  });

  it('应用 user preset → selectPlaygroundTextPresetById + selectPlaygroundVisionPresetById(id)', () => {
    const rec = makeRec();
    renderDialog(rec);

    fireEvent.change(screen.getByTestId('playground-scheme-select'), {
      target: { value: 'preset-1' },
    });
    expect(rec.selectPlaygroundTextPresetById).toHaveBeenCalledWith('preset-1');
    expect(rec.selectPlaygroundVisionPresetById).toHaveBeenCalledWith('preset-1');
  });
});

describe('Task 12b：全局搜索（三区过滤 + 不分页 + 即点即存不变）', () => {
  const searchBox = () => screen.getByTestId('playground-scope-search') as HTMLInputElement;

  it('搜索框存在（testid + aria-label）；非搜索态不影响既有分页渲染', () => {
    const rec = makeRec({
      entityTypes: WIDE_CATALOG,
      pipelines: [],
      selectedTypes: [],
      selectedOcrHasTypes: [],
      selectedVisualFeatureTypes: [],
    });
    renderDialog(rec);
    expect(searchBox()).not.toBeNull();
    expect(searchBox().getAttribute('aria-label')).toBe('搜索类型');
    // 搜索为空：翻页控件照常可点（20 类 3 页，第1页 → next 可用、prev 不可用）
    expect(screen.getByTestId('playground-scope-zone-ner-page-next').hasAttribute('disabled')).toBe(
      false,
    );
    expect(screen.getByTestId('playground-scope-zone-ner-page-prev').hasAttribute('disabled')).toBe(
      true,
    );
  });

  it('搜索态：三区同时过滤（显示名 or id）+ 匹配计数 + 翻页按钮禁用', () => {
    const rec = makeRec({
      entityTypes: WIDE_CATALOG,
      pipelines: [],
      selectedTypes: [],
      selectedOcrHasTypes: [],
      selectedVisualFeatureTypes: [],
    });
    renderDialog(rec);

    fireEvent.change(searchBox(), { target: { value: 'Type1' } });
    // NER 区命中 Type1/Type10..Type19 共 11 类，全部一次性渲染（不分页）
    for (const id of ['T01', 'T10', 'T19']) {
      expect(chip(id)).not.toBeNull();
    }
    expect(screen.queryByTestId('playground-scope-chip-T20')).toBeNull();
    expect(zone('ner').textContent).toContain('匹配 11 项');
    // 空管线的 ocr/la 两区 0 命中 → 无匹配空态，区框仍在
    expect(zone('ocr').textContent).toContain('无匹配');
    expect(zone('la').textContent).toContain('无匹配');
    // 翻页控件禁用
    expect(screen.getByTestId('playground-scope-zone-ner-page-next').hasAttribute('disabled')).toBe(
      true,
    );
    expect(screen.getByTestId('playground-scope-zone-ner-page-prev').hasAttribute('disabled')).toBe(
      true,
    );
  });

  it('搜索态按 id 小写匹配 + 有命中的第二区照常过滤（ocr 区命中 chip）', () => {
    const rec = makeRec();
    renderDialog(rec);
    fireEvent.change(searchBox(), { target: { value: 'ocr_addr' } });
    expect(chip('OCR_ADDR')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-chip-OCR_ID')).toBeNull();
    expect(zone('ocr').textContent).toContain('匹配 1 项');
    expect(zone('ner').textContent).toContain('无匹配');
    expect(zone('la').textContent).toContain('无匹配');
  });

  it('搜索态点 chip → 照样写 rec（即点即存不断：NER setter + vision toggle）', () => {
    const rec = makeRec({ selectedTypes: ['PERSON'] });
    renderDialog(rec);
    fireEvent.change(searchBox(), { target: { value: 'bank' } });
    expect(chip('BANK_CARD')).not.toBeNull();
    fireEvent.click(chip('BANK_CARD'));
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    const adder = vi.mocked(rec.setSelectedTypes).mock.calls[0][0] as (
      previous: string[],
    ) => string[];
    expect(adder(['PERSON'])).toEqual(['PERSON', 'BANK_CARD']);
  });

  // Issue#270 行为钉（裁决固化，不改行为）：搜索态下区「全选/全不选」仍作用于
  // 整区目录（selectZoneAll 用 ids=types 全量，不用 matchedTypes）。用户若反馈
  // 误选未显示项，届时收窄 matchedTypes 为有意行为变更——本钉变红即提醒重审。
  // 变异 M3（改用 matchedTypes）本钉必红。
  it('搜索态下区「全选/全不选」作用于整区目录而非匹配项（不收窄 matchedTypes）', () => {
    const rec = makeRec({
      entityTypes: WIDE_CATALOG,
      pipelines: [],
      selectedTypes: [],
      selectedOcrHasTypes: [],
      selectedVisualFeatureTypes: [],
    });
    renderDialog(rec);
    // 搜索 'Type1' 只显示 11 项命中（Type1/Type10..Type19），其余 9 项不可见
    fireEvent.change(searchBox(), { target: { value: 'Type1' } });
    expect(zone('ner').textContent).toContain('匹配 11 项');

    fireEvent.click(zoneAll('ner'));
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalled();
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    // 全选实参 = 整区全量 20 类（含 9 个未显示项），不是匹配子集
    const selectArg = vi.mocked(rec.setSelectedTypes).mock.calls[0][0] as string[];
    expect(selectArg).toHaveLength(20);
    expect([...selectArg].sort()).toEqual(
      WIDE_CATALOG.map((type) => type.id).sort(),
    );

    fireEvent.click(zoneNone('ner'));
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(2);
    expect(vi.mocked(rec.setSelectedTypes).mock.calls[1][0]).toEqual([]);
  });

  it('翻到第2页后搜索再清空 → 分页恢复且页码保持搜索前状态（第2/3页）', () => {
    const rec = makeRec({
      entityTypes: WIDE_CATALOG,
      pipelines: [],
      selectedTypes: [],
      selectedOcrHasTypes: [],
      selectedVisualFeatureTypes: [],
    });
    renderDialog(rec);
    fireEvent.click(screen.getByTestId('playground-scope-zone-ner-page-next'));
    expect(zone('ner').textContent).toContain('第2/3页');

    fireEvent.change(searchBox(), { target: { value: 'T01' } });
    expect(zone('ner').textContent).toContain('匹配 1 项');

    fireEvent.change(searchBox(), { target: { value: '' } });
    expect(zone('ner').textContent).toContain('第2/3页');
    expect(chip('T09')).not.toBeNull();
    expect(screen.queryByTestId('playground-scope-chip-T01')).toBeNull();
    expect(screen.getByTestId('playground-scope-zone-ner-page-next').hasAttribute('disabled')).toBe(
      false,
    );
  });

  it('离线区不参与搜索展示（保持暗区+红条）；na 区保持 na', () => {
    mockHealthState.health = makeHealth({ visual_features: 'offline' });
    const rec = makeRec({ selectedVisualFeatureTypes: ['OFFICIAL_SEAL'] });
    renderDialog(rec);
    fireEvent.change(searchBox(), { target: { value: '印章' } });
    // LA 离线：仍显示离线红条与暗区，不出 chip（搜索不作用于离线区）
    expect(zone('la').className).toContain('grayscale');
    expect(zone('la').textContent).toContain('本区暂不参与识别，恢复后自动回来');
    expect(screen.queryByTestId('playground-scope-chip-OFFICIAL_SEAL')).toBeNull();
    // NER 区照常过滤（印章在 NER 无命中）
    expect(zone('ner').textContent).toContain('无匹配');
    // OCR 区在线且有命中 → 正常过滤展示
    expect(zone('ocr').textContent).toContain('无匹配');
  });

  it('na 区在搜索态保持 na（na 优先于搜索：纯文本文件 OCR/LA 区不参与）', () => {
    const rec = makeRec({ selectedVisualFeatureTypes: ['OFFICIAL_SEAL'] });
    renderDialog(rec, { visionApplicable: false });
    fireEvent.change(searchBox(), { target: { value: '印章' } });
    // OCR/LA 区保持不适用文案、不暗化；不出 chip
    expect(zone('ocr').textContent).toContain('本文件是纯文本，不走此区');
    expect(zone('la').textContent).toContain('本文件是纯文本，不走此区');
    expect(zone('la').className).not.toContain('grayscale');
    expect(screen.queryByTestId('playground-scope-chip-OFFICIAL_SEAL')).toBeNull();
    // NER 区照常过滤（印章在 NER 无命中）
    expect(zone('ner').textContent).toContain('无匹配');
  });
});

describe('Task 10b：保存为方案（createPreset + 列表失效 + 自动套用）', () => {
  it('点保存为方案 → 内联表单出现；空名 → 错误 toast、不调 createPreset', () => {
    const rec = makeRec();
    renderDialog(rec);

    fireEvent.click(screen.getByTestId('playground-scheme-save'));
    const input = screen.getByTestId('playground-scheme-save-input') as HTMLInputElement;
    expect(input).not.toBeNull();

    fireEvent.click(screen.getByTestId('playground-scheme-save-confirm'));
    expect(createPresetMock).not.toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledTimes(1);
    expect(vi.mocked(showToast).mock.calls[0][1]).toBe('error');
  });

  it('填名确认 → createPreset(kind full+三清单+replacementMode) + invalidatePresets + 成功 toast + 表单收起', async () => {
    createPresetMock.mockResolvedValue({ ...USER_PRESET, id: 'preset-new', name: '执行案件初查' });
    const rec = makeRec({ replacementMode: 'smart' });
    renderDialog(rec);

    fireEvent.click(screen.getByTestId('playground-scheme-save'));
    fireEvent.change(screen.getByTestId('playground-scheme-save-input'), {
      target: { value: '执行案件初查' },
    });
    fireEvent.click(screen.getByTestId('playground-scheme-save-confirm'));

    await waitFor(() => {
      expect(createPresetMock).toHaveBeenCalledTimes(1);
    });
    expect(createPresetMock).toHaveBeenCalledWith({
      name: '执行案件初查',
      kind: 'full',
      selectedEntityTypeIds: LEGAL_IDS,
      ocrHasTypes: OCR_DEFAULT_IDS,
      visualFeatureTypes: VISUAL_DEFAULT_IDS,
      replacementMode: 'smart',
    });
    await waitFor(() => {
      expect(invalidatePresetsMock).toHaveBeenCalledTimes(1);
    });
    expect(showToast).toHaveBeenCalledWith(
      expect.stringContaining('执行案件初查'),
      'success',
    );
    await waitFor(() => {
      expect(screen.queryByTestId('playground-scheme-save-input')).toBeNull();
    });
  });

  it('保存成功且列表刷新同步后 → 自动套用（selectPlaygroundText/VisionPresetById 新 id）', async () => {
    createPresetMock.mockResolvedValue({ ...USER_PRESET, id: 'preset-new', name: '执行案件初查' });
    const rec = makeRec();
    const ctx = { rec };
    const { rerender } = render(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={rec} visionApplicable />,
    );

    fireEvent.click(screen.getByTestId('playground-scheme-save'));
    fireEvent.change(screen.getByTestId('playground-scheme-save-input'), {
      target: { value: '执行案件初查' },
    });
    fireEvent.click(screen.getByTestId('playground-scheme-save-confirm'));
    await waitFor(() => {
      expect(invalidatePresetsMock).toHaveBeenCalled();
    });

    // react-query 刷新 → rec 内部清单同步出新建 preset（full kind 两侧都在）→ effect 自动套用
    const created = { ...USER_PRESET, id: 'preset-new', name: '执行案件初查' };
    ctx.rec.textPresetsPg = [created];
    ctx.rec.visionPresetsPg = [created];
    rerender(
      <RecognitionScopeDialog open onOpenChange={vi.fn()} rec={ctx.rec} visionApplicable />,
    );
    await waitFor(() => {
      expect(ctx.rec.selectPlaygroundTextPresetById).toHaveBeenCalledWith('preset-new');
      expect(ctx.rec.selectPlaygroundVisionPresetById).toHaveBeenCalledWith('preset-new');
    });
  });

  it('createPreset 失败 → 错误 toast、不套用', async () => {
    createPresetMock.mockRejectedValue(new Error('boom'));
    const rec = makeRec();
    renderDialog(rec);

    fireEvent.click(screen.getByTestId('playground-scheme-save'));
    fireEvent.change(screen.getByTestId('playground-scheme-save-input'), {
      target: { value: '会失败的方案' },
    });
    fireEvent.click(screen.getByTestId('playground-scheme-save-confirm'));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith(expect.any(String), 'error');
    });
    expect(rec.selectPlaygroundTextPresetById).not.toHaveBeenCalled();
  });
});
