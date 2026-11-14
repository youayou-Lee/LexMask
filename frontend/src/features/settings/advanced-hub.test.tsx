// Copyright 2026 LexMask Contributors

// Issue#262 D3：/settings 单入口三页签（types | redaction | words）。
// 本测试的被测对象是外层页签容器：URL ?tab= 缺省 types、URL→tab 与 tab→URL
// 双向同步、非法值回落、三个页签内容各自可达。
// 识别项页签内部（text/vision）与 RedactionList / WordPoolsSettings 均整体
// mock 为占位组件——它们的内部逻辑各有归属（use-entity-types 链 / 预设清单 /
// 词库 CRUD），不属于本任务测试面（参考 entity-type-list.clamp-tooltip.test.tsx
// 的 jsdom 切换与 mock 惯例）。
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { SettingsHub } from './settings-hub';

// Issue#267：mock 工厂改可变 holder——loading/loadError 可按用例注入，
// fetch spy 可断言重试调用（原静态 mock 无法覆盖失败态）。
const mockHubState = vi.hoisted(() => ({
  loading: false,
  loadError: null as string | null,
  fetchEntityTypes: vi.fn(),
  fetchPipelines: vi.fn(),
}));

vi.mock('./hooks/use-entity-types', () => {
  const noop = () => undefined;
  return {
    useEntityTypes: () => ({
      entityTypes: [],
      textTaxonomy: [],
      pipelines: [],
      loading: mockHubState.loading,
      pipelinesLoading: false,
      regexTypes: [],
      textRuleTypes: [],
      loadError: mockHubState.loadError,
      importFileRef: { current: null },
      createType: noop,
      updateType: noop,
      updateTypeOverride: noop,
      deleteType: noop,
      resetToDefault: noop,
      createPipelineType: noop,
      updatePipelineType: noop,
      deletePipelineType: noop,
      resetPipelines: noop,
      handleExportPresets: noop,
      handleImportPresets: noop,
      fetchEntityTypes: mockHubState.fetchEntityTypes,
      fetchPipelines: mockHubState.fetchPipelines,
    }),
  };
});

vi.mock('./components/entity-type-list', () => ({
  EntityTypeList: () => <div data-testid="mock-entity-type-list" />,
}));
vi.mock('./components/entity-type-dialog', () => ({
  EntityTypeDialog: () => null,
}));
vi.mock('./components/pipeline-config', () => ({
  PipelineConfigPanel: () => <div data-testid="mock-pipeline-config" />,
}));
vi.mock('./redaction-list', () => ({
  RedactionList: () => <div data-testid="mock-redaction-list" />,
}));
vi.mock('./word-pools', () => ({
  WordPoolsSettings: () => <div data-testid="mock-word-pools" />,
}));

/** 观察当前 URL：tab→URL 回写的证据侧（与 use-history-data 的 searchParams 同步范式对应） */
function LocationProbe() {
  const location = useLocation();
  return (
    <div
      data-testid="location-probe"
      data-pathname={location.pathname}
      data-search={location.search}
    />
  );
}

function renderHub(initialEntry: string) {
  return render(
    <MemoryRouter initialEntries={[initialEntry]}>
      <LocationProbe />
      <SettingsHub />
    </MemoryRouter>,
  );
}

/** 激活页签：Radix Tabs.Trigger 的激活事件是 onMouseDown（button=0、非 ctrl） */
function activateTab(testId: string) {
  fireEvent.mouseDown(screen.getByTestId(testId), { button: 0 });
}

afterEach(() => {
  cleanup();
  mockHubState.loading = false;
  mockHubState.loadError = null;
  mockHubState.fetchEntityTypes.mockClear();
  mockHubState.fetchPipelines.mockClear();
});

describe('Issue#262：高级设置单入口三页签', () => {
  it('缺省（无 ?tab=）渲染识别项设置页签，URL 不带 tab 参数', () => {
    renderHub('/settings');
    expect(screen.getByTestId('settings-tab-types').getAttribute('aria-selected')).toBe('true');
    expect(screen.getByTestId('settings-tab-redaction').getAttribute('aria-selected')).toBe(
      'false',
    );
    expect(screen.getByTestId('settings-tab-words').getAttribute('aria-selected')).toBe('false');
    // 识别项内容挂载；其余两页签内容不挂载（Radix inactive content 卸载）
    expect(screen.queryByTestId('mock-entity-type-list')).toBeTruthy();
    expect(screen.queryByTestId('mock-redaction-list')).toBeNull();
    expect(screen.queryByTestId('mock-word-pools')).toBeNull();
    // 缺省页签不污染 URL
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('');
  });

  it('?tab=words 直达词库页签（URL→tab）', () => {
    renderHub('/settings?tab=words');
    expect(screen.getByTestId('settings-tab-words').getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByTestId('mock-word-pools')).toBeTruthy();
    expect(screen.queryByTestId('mock-entity-type-list')).toBeNull();
    expect(screen.queryByTestId('mock-redaction-list')).toBeNull();
  });

  it('?tab=redaction 直达配置清单页签（URL→tab，深链等效旧路由）', () => {
    renderHub('/settings?tab=redaction');
    expect(screen.getByTestId('settings-tab-redaction').getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByTestId('mock-redaction-list')).toBeTruthy();
    expect(screen.queryByTestId('mock-entity-type-list')).toBeNull();
  });

  it('点击页签切换内容并回写 URL（tab→URL）', () => {
    renderHub('/settings');
    // Radix Tabs.Trigger 在 onMouseDown（button=0）激活，fireEvent.click 不触发
    activateTab('settings-tab-redaction');
    expect(screen.getByTestId('settings-tab-redaction').getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByTestId('mock-redaction-list')).toBeTruthy();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('?tab=redaction');

    activateTab('settings-tab-words');
    expect(screen.queryByTestId('mock-word-pools')).toBeTruthy();
    expect(screen.queryByTestId('mock-redaction-list')).toBeNull();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('?tab=words');

    // 切回缺省页签：?tab= 移除（与 history 钩子「all → delete 参数」的同步惯例一致）
    activateTab('settings-tab-types');
    expect(screen.queryByTestId('mock-entity-type-list')).toBeTruthy();
    expect(screen.getByTestId('location-probe').getAttribute('data-search')).toBe('');
  });

  it('非法 ?tab= 值回落缺省页签', () => {
    renderHub('/settings?tab=bogus');
    expect(screen.getByTestId('settings-tab-types').getAttribute('aria-selected')).toBe('true');
  });

  it('既有识别项内部页签 testid（settings-tabs/tab-text/tab-vision/export-presets/import-presets）仍在识别项页签内可达', () => {
    renderHub('/settings');
    expect(screen.queryByTestId('settings-tabs')).toBeTruthy();
    expect(screen.queryByTestId('tab-text')?.getAttribute('aria-selected')).toBe('true');
    expect(screen.queryByTestId('tab-vision')).toBeTruthy();
    expect(screen.queryByTestId('export-presets')).toBeTruthy();
    expect(screen.queryByTestId('import-presets')).toBeTruthy();
  });
});

// Issue#267：hub 首帧与页签失败态——types 目录 hook 下沉进 types 页签面板后：
// ① loading 不再遮页签栏（页签栏常驻、深链首帧不被 types fetch 阻塞）；
// ② loadError Alert 只属于 types 页签（redaction/words 深链看不到）；
// ③ Alert 重试按钮重发两目录请求；
// ④ 再进入 types = 重取 + 面板内 spinner（F1 拍板，A7 renderHook 钉）。
describe('Issue#267：hub 首帧与页签失败态', () => {
  it('A1：loading=true 时页签栏仍渲染且可切往 redaction；types 区处于面板内 spinner 态', () => {
    mockHubState.loading = true;
    renderHub('/settings');
    // 页签栏不被 types 目录加载遮住（HEAD 现状：整 hub spinner，此断言红）
    expect(screen.getByTestId('settings-hub-tabs')).toBeTruthy();
    expect(screen.queryByTestId('mock-entity-type-list')).toBeNull();
    activateTab('settings-tab-redaction');
    expect(screen.queryByTestId('mock-redaction-list')).toBeTruthy();
    expect(screen.queryByTestId('mock-entity-type-list')).toBeNull();
  });

  it('A2：loadError + ?tab=redaction 深链 → types 失败 Alert 不在文档、redaction 内容可见', () => {
    mockHubState.loadError = '设置加载失败，请检查后端服务或稍后重试。';
    renderHub('/settings?tab=redaction');
    expect(screen.queryByTestId('settings-load-error')).toBeNull();
    expect(screen.queryByTestId('mock-redaction-list')).toBeTruthy();
  });

  it('A3：loadError + 默认页签 → Alert 可见含重试按钮；点击重试调两 fetch 各恰一次', () => {
    mockHubState.loadError = '设置加载失败，请检查后端服务或稍后重试。';
    renderHub('/settings');
    const alert = screen.getByTestId('settings-load-error');
    expect(alert.textContent).toContain('设置加载失败');
    fireEvent.click(screen.getByTestId('settings-load-error-retry'));
    expect(mockHubState.fetchEntityTypes).toHaveBeenCalledTimes(1);
    expect(mockHubState.fetchPipelines).toHaveBeenCalledTimes(1);
  });

  it('A6：?tab=words 深链在 loading=true 下 words 内容直接可达（首帧不被 types fetch 阻塞）', () => {
    mockHubState.loading = true;
    renderHub('/settings?tab=words');
    expect(screen.queryByTestId('mock-word-pools')).toBeTruthy();
    expect(screen.queryByTestId('mock-entity-type-list')).toBeNull();
    expect(screen.queryByTestId('settings-load-error')).toBeNull();
  });
});
