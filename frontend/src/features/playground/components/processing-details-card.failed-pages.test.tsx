// Copyright 2026 LexMask Contributors

// Issue#199：识别失败页的可见性（泄漏安全 AC）。
// - lib：recognitionFailedPages 解析 performance.recognition.failed_pages；
// - 卡片：失败页警告行渲染（amber 警示，非 destructive 失败态）；
// - 空 failed_pages / 缺键 → 不渲染警告行（成功路径零变化）。

// @vitest-environment jsdom

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';
import { recognitionFailedPages } from '../lib/processing-details';
import { ProcessingDetailsCard } from './processing-details-card';

describe('recognitionFailedPages', () => {
  it('parses failed_pages keys ascending', () => {
    expect(
      recognitionFailedPages({ recognition: { failed_pages: { '3': 'x', '1': 'y' } } }),
    ).toEqual([1, 3]);
  });

  it('treats empty object and missing key as no failures', () => {
    expect(recognitionFailedPages({ recognition: { failed_pages: {} } })).toEqual([]);
    expect(recognitionFailedPages({ recognition: {} })).toEqual([]);
    expect(recognitionFailedPages(null)).toEqual([]);
    expect(recognitionFailedPages({ recognition: { failed_pages: 'garbage' } })).toEqual([]);
  });
});

describe('ProcessingDetailsCard failed-pages warning', () => {
  it('renders warning row with page list when failures exist', () => {
    render(
      <ProcessingDetailsCard
        performance={{
          recognition: {
            duration_ms: 1000,
            failed_pages: { '133': 'timeout', '5': 'ValueError' },
            pages: { '5': { status: 'completed' } },
          },
        }}
      />,
    );
    const row = screen.getByTestId('processing-details-failed-pages');
    expect(row.textContent).toContain('5');
    expect(row.textContent).toContain('133');
  });

  it('renders no warning row on clean success', () => {
    render(
      <ProcessingDetailsCard
        performance={{
          recognition: {
            duration_ms: 1000,
            failed_pages: {},
            pages: { '1': { status: 'completed' } },
          },
        }}
      />,
    );
    expect(screen.queryByTestId('processing-details-failed-pages')).toBeNull();
  });

  it('still renders card with only failed pages and no stage rows', () => {
    render(
      <ProcessingDetailsCard performance={{ recognition: { failed_pages: { '7': 'timeout' } } }} />,
    );
    expect(screen.getByTestId('processing-details-failed-pages')).toBeTruthy();
  });
});
