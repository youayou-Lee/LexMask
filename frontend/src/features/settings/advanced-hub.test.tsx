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

vi.mock('./hooks/use-entity-types', () => {
  const noop = () => undefined;
  return {
    useEntityTypes: () => ({
      entityTypes: [],
      textTaxonomy: [],
      pipelines: [],
      loading: false,
      pipelinesLoading: false,
      regexTypes: [],
      textRuleTypes: [],
      loadError: null,
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
      fetchEntityTypes: noop,
      fetchPipelines: noop,
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
