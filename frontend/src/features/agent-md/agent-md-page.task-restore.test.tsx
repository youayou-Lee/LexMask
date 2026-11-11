// Copyright 2026 LexMask Contributors

// Issue#282：/agent-md?taskId= 深链恢复——历史/任务中心点进来直接回到评审步。
// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { MemoryRouter } from 'react-router-dom';

vi.mock('./api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./api')>();
  return {
    ...actual,
    agentMdApi: {
      ...actual.agentMdApi,
      status: vi.fn(),
      mapping: vi.fn(),
    },
  };
});

import { agentMdApi } from './api';
import { AgentMd, ACTIVE_TASK_KEY } from './agent-md-page';

const statusMock = vi.mocked(agentMdApi.status);
const mappingMock = vi.mocked(agentMdApi.mapping);

function statusBody(state: string) {
  return {
    task_id: 't-42',
    state,
    stage: 'ner',
    pages_done: 3,
    pages_total: 3,
    message: '',
    warnings_count: 0,
  };
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  statusMock.mockReset();
  mappingMock.mockReset();
});

describe('agent-md taskId restore', () => {
  it('mapping_ready 直接进评审步，并写入活跃任务键', async () => {
    statusMock.mockResolvedValue(statusBody('mapping_ready'));
    mappingMock.mockResolvedValue({
      items: [
        {
          id: 'e1',
          original_text: '张三',
          entity_type: 'PERSON',
          replacement: '张某',
          excluded: false,
        },
      ],
    });

    render(
      <MemoryRouter initialEntries={['/agent-md?taskId=t-42']}>
        <AgentMd />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId('agent-md-step-review')).toBeTruthy());
    expect(statusMock).toHaveBeenCalledWith('t-42');
    expect(mappingMock).toHaveBeenCalledWith('t-42');
    expect(localStorage.getItem(ACTIVE_TASK_KEY)).toBe('t-42');
    expect(screen.getByTestId('agent-md-row-0')).toBeTruthy();
  });

  it('未知任务 404 时回上传步并提示过期', async () => {
    statusMock.mockRejectedValue(new Error('404'));
    render(
      <MemoryRouter initialEntries={['/agent-md?taskId=gone']}>
        <AgentMd />
      </MemoryRouter>,
    );

    await waitFor(() => expect(screen.getByTestId('agent-md-page')).toBeTruthy());
    await waitFor(() => expect(statusMock).toHaveBeenCalled());
    expect(localStorage.getItem(ACTIVE_TASK_KEY)).toBeNull();
  });
});
