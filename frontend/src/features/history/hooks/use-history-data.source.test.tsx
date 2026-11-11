// Copyright 2026 LexMask Contributors

// Issue#282：?source=agent_md 必须作为列表请求参数下发（而非只在 UI 层过滤）。
// @vitest-environment jsdom

import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';

const { listMock } = vi.hoisted(() => ({ listMock: vi.fn() }));

vi.mock('@/services/api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/services/api')>();
  return {
    ...actual,
    fileApi: { ...actual.fileApi, list: listMock },
  };
});

import { parseSourceTab, useHistoryData } from './use-history-data';

function wrapper({ children }: { children: ReactNode }) {
  return (
    <MemoryRouter initialEntries={['/jobs?view=done&source=agent_md']}>{children}</MemoryRouter>
  );
}

afterEach(() => listMock.mockReset());

describe('useHistoryData agent_md source', () => {
  it('parseSourceTab 只认四个来源值', () => {
    expect(parseSourceTab('agent_md')).toBe('agent_md');
    expect(parseSourceTab('batch')).toBe('batch');
    expect(parseSourceTab('bogus')).toBe('all');
    expect(parseSourceTab(null)).toBe('all');
  });

  it('URL source=agent_md 时以 agent_md 请求列表', async () => {
    listMock.mockResolvedValue({ files: [], total: 0, page: 1, page_size: 10, stats: {} });
    renderHook(() => useHistoryData(), { wrapper });
    await waitFor(() => expect(listMock).toHaveBeenCalled());
    expect(listMock.mock.calls[0][2]).toMatchObject({ source: 'agent_md', embed_job: true });
  });
});
