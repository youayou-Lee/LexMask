// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

import { filterHiddenNavItems } from './app-sidebar';

const workflowPaths = [
  '/',
  '/single',
  '/batch',
  '/structured',
  '/dicom',
  '/restore',
  '/jobs',
  '/history',
];

describe('filterHiddenNavItems (Issue#78 侧边栏暂时隐藏入口)', () => {
  it('隐藏「库表处理」与「医学影像 DICOM」两个一级入口', () => {
    const paths = filterHiddenNavItems(workflowPaths.map((path) => ({ path }))).map(
      (item) => item.path,
    );
    expect(paths).not.toContain('/structured');
    expect(paths).not.toContain('/dicom');
  });

  it('保留处理流程其余项：开始/单次/批量/还原工具/任务中心/处理结果', () => {
    const paths = filterHiddenNavItems(workflowPaths.map((path) => ({ path }))).map(
      (item) => item.path,
    );
    expect(paths).toEqual(['/', '/single', '/batch', '/restore', '/jobs', '/history']);
  });

  it('按 path 精确匹配，不误伤前缀相近的其他条目', () => {
    const paths = filterHiddenNavItems(
      ['/structured', '/structuredx', '/structured-other', '/jobs'].map((path) => ({ path })),
    ).map((item) => item.path);
    expect(paths).toEqual(['/structuredx', '/structured-other', '/jobs']);
  });

  it('不改变未受影响条目的顺序与引用', () => {
    const items = [{ path: '/batch' }, { path: '/dicom' }, { path: '/jobs' }];
    const result = filterHiddenNavItems(items);
    expect(result).toHaveLength(2);
    expect(result[0]).toBe(items[0]);
    expect(result[1]).toBe(items[2]);
  });
});
