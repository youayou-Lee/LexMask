// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render } from '@testing-library/react';
import { resolveLegalTextSelection } from '@/config/legalPreset';
import { buildDefaultTextTypeIds } from '@/services/defaultRedactionPreset';
import type { usePlayground } from '../hooks/use-playground';
import type { EntityTypeConfig } from '../types';
import type { ConfigLoadState } from '../lib/recognition-config';
import { PlaygroundUpload } from './playground-upload';

/**
 * Issue#262 终审（Task 9 finding 2）：识别前「法律卷宗默认选中」挂载 effect 的组件级钉。
 *
 * 被测实现：playground-upload.tsx 的 legalAppliedForRef 挂载 effect——
 * 哨兵（playgroundPresetTextId ?? '__default__' === '__default__'，运行时 null 即默认清单）
 * + selection==后端默认集前置条件 + entityTypes 引用键 re-arm ref 守卫。
 * rec mock 逐键对齐 use-playground-recognition.ts 返回形状（本分支 651-701 行）。
 */

vi.mock('@/hooks/use-service-health', () => ({
  useServiceHealth: () => ({
    health: null,
    checking: false,
    roundTripMs: null,
    refresh: vi.fn(),
  }),
}));

// Issue#267 C3：高级入口打开识别范围三区弹窗——弹窗内部跑 usePresets（react-query），
// jsdom 无 QueryClient，按 reskin 测试的稳定引用约定 mock。
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
const CATALOG_A: EntityTypeConfig[] = [
  { id: 'PERSON', name: 'Person', color: '#000000', enabled: true, default_enabled: true },
  { id: 'PHONE', name: 'Phone', color: '#000000', enabled: true, default_enabled: true },
  { id: 'CASE_NUMBER', name: 'CaseNumber', color: '#000000', enabled: true, default_enabled: true },
  { id: 'MED_PATIENT', name: 'Patient', color: '#000000', enabled: true, default_enabled: false },
  { id: 'BANK_CARD', name: 'BankCard', color: '#000000', enabled: true, default_enabled: false },
];
// 用例 C 用的「重取后目录」：内容同 A、引用不同（模拟 loadRecognitionConfig 重取换数组）。
const CATALOG_A_REFETCHED = CATALOG_A.map((type) => ({ ...type }));

const BACKEND_DEFAULT_IDS = buildDefaultTextTypeIds(CATALOG_A);
const LEGAL_IDS = resolveLegalTextSelection(CATALOG_A.map((type) => type.id));

function makeRec(overrides: {
  entityTypes: EntityTypeConfig[];
  selectedTypes: string[];
  playgroundPresetTextId?: string | null;
  /** Issue#267：缺省 'ready'；目录态变体（loading/empty/unavailable）须显式覆盖 */
  textConfigState?: ConfigLoadState;
}): RecognitionCtx {
  return {
    entityTypes: overrides.entityTypes,
    textConfigState: overrides.textConfigState ?? 'ready',
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

afterEach(() => {
  cleanup();
});

describe('Issue#262 终审：法律卷宗默认选中 effect（组件钉）', () => {
  it('A：selection==后端默认集且 playgroundPresetTextId=null → 挂载后 setSelectedTypes 恰一次、实参=法律卷宗集', () => {
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...BACKEND_DEFAULT_IDS],
    });
    render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    expect(rec.setSelectedTypes).toHaveBeenCalledWith(LEGAL_IDS);
    // 具体值钉：目录交集按 LEGAL_TEXT_TYPE_IDS 顺序 = PERSON/PHONE/BANK_CARD/CASE_NUMBER
    expect(LEGAL_IDS).toEqual(['PERSON', 'PHONE', 'BANK_CARD', 'CASE_NUMBER']);
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalledTimes(1);
  });

  it('B：同 entityTypes 引用重渲染（选择被重置回后端默认集）→ 不再调用（ref 守卫）', () => {
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...BACKEND_DEFAULT_IDS],
    });
    const ctx = makeCtx(rec);
    const { rerender } = render(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    // 同一目录引用下，选择又被外部重置回后端默认集（新数组实例）→ ref 守卫拦下
    rec.selectedTypes = [...BACKEND_DEFAULT_IDS];
    rerender(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    expect(rec.clearPlaygroundTextPresetTracking).toHaveBeenCalledTimes(1);
  });

  // Issue#270 存照（产品已裁定可接受）：本用例即「显式选回默认清单后遇 refetch 被
  // 法律默认覆盖」的现状钉——触发条件 = tracking=null（哨兵）且 selectedTypes 恰等
  // 后端 default_enabled 集（法律集≠默认集，两条 refetch 路径均 preserve selection，
  // 不会先把选择重置回默认集）。产品裁定不改；本钉变红 = 有意行为变更，需重审。
  it('C：新 entityTypes 数组引用（重取换目录）+ selection 重置回后端默认集 → 恰再调用一次（re-arm）', () => {
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...BACKEND_DEFAULT_IDS],
    });
    const ctx = makeCtx(rec);
    const { rerender } = render(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(1);
    rec.entityTypes = CATALOG_A_REFETCHED;
    rec.selectedTypes = [...BACKEND_DEFAULT_IDS];
    rerender(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).toHaveBeenCalledTimes(2);
    expect(rec.setSelectedTypes).toHaveBeenLastCalledWith(LEGAL_IDS);
  });

  it('D：playgroundPresetTextId 指向自定义清单 → 任何情况不调用（不打扰用户已选清单）', () => {
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: [...BACKEND_DEFAULT_IDS],
      playgroundPresetTextId: 'preset-1',
    });
    const ctx = makeCtx(rec);
    const { rerender } = render(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).not.toHaveBeenCalled();
    // 重渲染 + 选择回到后端默认集（哨兵语义下唯一会触发的形态）→ 仍不调用
    rec.selectedTypes = [...BACKEND_DEFAULT_IDS];
    rerender(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).not.toHaveBeenCalled();
    expect(rec.clearPlaygroundTextPresetTracking).not.toHaveBeenCalled();
  });

  it('E：selection 已偏离后端默认集（用户已改选）→ 不应用', () => {
    const rec = makeRec({
      entityTypes: CATALOG_A,
      selectedTypes: ['PERSON'],
    });
    const ctx = makeCtx(rec);
    const { rerender } = render(<PlaygroundUpload ctx={ctx} />);
    rec.selectedTypes = ['PERSON', 'MED_PATIENT'];
    rerender(<PlaygroundUpload ctx={ctx} />);
    expect(rec.setSelectedTypes).not.toHaveBeenCalled();
  });

  it('F：目录为空（配置未加载）→ 不应用', () => {
    const rec = makeRec({ entityTypes: [], selectedTypes: [] });
    render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    expect(rec.setSelectedTypes).not.toHaveBeenCalled();
  });
});

// Issue#267：目录未就绪时法律/全部 chip 门控——空目录下 resolveLegalTextSelection
// 回落空集（legalPreset.ts fail-open 分支），点 chip 会把 selectedTypes 写成 []，
// 提交识别时文本侧零类型。门控两层：disabled（UI 面）+ handler 守卫（写面）。
describe('Issue#267：目录未就绪 chip 门控（法律/全部 chip 不写空集）', () => {
  const chipLegal = (container: HTMLElement) =>
    container.querySelector('[data-testid="playground-preset-chip-legal"]') as HTMLButtonElement;
  const chipAll = (container: HTMLElement) =>
    container.querySelector('[data-testid="playground-preset-chip-all"]') as HTMLButtonElement;

  it('C1a：textConfigState=loading（目录空）→ 两颗 chip disabled=true 且 title 指向目录态', () => {
    const rec = makeRec({ entityTypes: [], selectedTypes: [], textConfigState: 'loading' });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    expect(chipLegal(container).disabled).toBe(true);
    expect(chipAll(container).disabled).toBe(true);
    // title 提示把「为什么置灰」讲清楚（zh：识别项目录加载中…）
    expect(chipLegal(container).title).toContain('加载');
  });

  it('C1b：人工组合 textConfigState=ready + entityTypes=[]（运行时不存在态）→ chip 可点但 handler 守卫拦下零写', () => {
    const rec = makeRec({ entityTypes: [], selectedTypes: [], textConfigState: 'ready' });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    expect(chipLegal(container).disabled).toBe(false);
    expect(chipAll(container).disabled).toBe(false);
    fireEvent.click(chipLegal(container));
    fireEvent.click(chipAll(container));
    // 守卫：目录数组为空时宁可不动，不写空集
    expect(rec.setSelectedTypes).not.toHaveBeenCalled();
    expect(rec.clearPlaygroundTextPresetTracking).not.toHaveBeenCalled();
  });

  it('C3：目录未就绪（loading）时高级入口不受门控——不 disabled、点击仍开识别范围弹窗', () => {
    const rec = makeRec({ entityTypes: [], selectedTypes: [], textConfigState: 'loading' });
    const { container } = render(<PlaygroundUpload ctx={makeCtx(rec)} />);
    const entry = container.querySelector('[data-testid="playground-scope-entry"]') as HTMLButtonElement;
    expect(entry.disabled).toBe(false);
    fireEvent.click(entry);
    // 弹窗按需挂载：打开后 playground-scope-dialog 出现（用户能进弹窗看到目录态并重试）
    expect(
      container.ownerDocument.querySelector('[data-testid="playground-scope-dialog"]'),
    ).not.toBeNull();
  });
});
