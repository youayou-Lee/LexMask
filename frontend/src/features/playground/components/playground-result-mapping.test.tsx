// Copyright 2026 LexMask Contributors

// Issue#312 AC1：映射栏表头同时展示「去重条数 / 实际打码处数」。

// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { useI18n } from '@/i18n';
import { MappingColumn } from './playground-result-mapping';

const baseProps = {
  entityMap: { 张三: '已打码', 李四: '已打码' } as Record<string, string>,
  origToTypeId: new Map<string, string>([
    ['张三', 'PERSON'],
    ['李四', 'PERSON'],
  ]),
  scrollToMatch: vi.fn(),
  versionHistory: [],
  versionHistoryOpen: false,
  setVersionHistoryOpen: vi.fn(),
  mobileTab: 'mapping',
};

beforeEach(() => {
  useI18n.setState({ locale: 'zh' });
});
afterEach(cleanup);

describe('MappingColumn 计数口径（Issue#312）', () => {
  it('提供 redactedCount 时表头渲染「N 条 / M 处」', () => {
    render(<MappingColumn {...baseProps} redactedCount={5} />);
    expect(screen.getByTestId('mapping-count').textContent).toBe('2 条 / 5 处');
  });

  it('未提供 redactedCount 时保持旧的单数字口径', () => {
    render(<MappingColumn {...baseProps} />);
    expect(screen.getByTestId('mapping-count').textContent).toBe('2');
  });
});
