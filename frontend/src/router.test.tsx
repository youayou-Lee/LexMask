// Copyright 2026 LexMask Contributors

// Issue#262 A1：/history 旧深链重定向渲染钉——MemoryRouter 直渲染 HistoryRedirect，
// 断言目标含 view=done 且既有 query（source/jobId）原样透传
// （写法参照 tasks-page.test.tsx 的 LocationProbe 范式）。

// @vitest-environment jsdom

import { cleanup, render, screen } from '@testing-library/react';
import { MemoryRouter, useLocation } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { HistoryRedirect } from './router';

function LocationProbe() {
  const location = useLocation();
  return <div data-testid="probe" data-to={`${location.pathname}${location.search}`} />;
}

describe('Issue#262：/history 深链重定向', () => {
  afterEach(() => cleanup());

  it('渲染即重定向 /jobs?view=done，既有 query 原样透传', () => {
    render(
      <MemoryRouter initialEntries={['/history?source=batch&jobId=j-1']}>
        <HistoryRedirect />
        <LocationProbe />
      </MemoryRouter>,
    );
    expect(screen.getByTestId('probe').getAttribute('data-to')).toBe(
      '/jobs?source=batch&jobId=j-1&view=done',
    );
  });
});
