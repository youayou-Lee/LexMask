// Copyright 2026 LexMask Contributors

// @vitest-environment jsdom

// Issue#355 iOS 重置：高级设置页 ds 大标题页头钉（真稿 ios-settings .page-head）。
// - ds 下渲染大标题页头（ds-settings-header）+ iOS 分段页签样式钩子；
// - classic 零新 DOM：无页头元素（页签容器两态都在，ios-segmented 为
//   无害样式钩子——classic 无对应 CSS 规则，视觉零漂移）。
// 重子组件 mock 惯例与 advanced-hub.test.tsx 一致（本文件只测外层容器）。

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

import { useUiTheme } from '@/theme/ui-theme';

import { SettingsHub } from './settings-hub';

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

function renderHub() {
  return render(
    <MemoryRouter>
      <SettingsHub />
    </MemoryRouter>,
  );
}

afterEach(() => {
  cleanup();
  mockHubState.loading = false;
  mockHubState.loadError = null;
  useUiTheme.setState({ theme: 'classic' });
});

describe('Issue#355 ds 设置页大标题页头', () => {
  it('ds 下渲染大标题页头（h1 + 副标题）且页签带 iOS 分段样式钩子', () => {
    useUiTheme.setState({ theme: 'ds' });
    renderHub();
    const header = screen.getByTestId('ds-settings-header');
    expect(header.querySelector('h1')).toBeTruthy();
    expect(header.querySelector('.ds-page-sub')).toBeTruthy();
    expect(screen.getByTestId('settings-hub-tabs').classList.contains('ios-segmented')).toBe(
      true,
    );
    // 三个页签仍可达（结构性回归钉）
    expect(screen.getByTestId('settings-tab-types')).toBeTruthy();
    expect(screen.getByTestId('settings-tab-redaction')).toBeTruthy();
    expect(screen.getByTestId('settings-tab-words')).toBeTruthy();
  });

  it('classic 无页头 DOM（零新 DOM）；页签容器两态都在', () => {
    useUiTheme.setState({ theme: 'classic' });
    renderHub();
    expect(screen.queryByTestId('ds-settings-header')).toBeNull();
    expect(document.querySelector('.ds-page-title')).toBeNull();
    expect(screen.getByTestId('settings-hub-tabs')).toBeTruthy();
    expect(screen.getByTestId('settings-tab-types')).toBeTruthy();
  });
});
