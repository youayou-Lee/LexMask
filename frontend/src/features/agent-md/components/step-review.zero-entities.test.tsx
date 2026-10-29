// Copyright 2026 LexMask Contributors

// Issue#212 AC5（前端接线判据）：0 实体时「确认生成」必须先勾选知情，且提交带
// acknowledge_zero_entities —— 只测 helper 不算（#159 教训：删除页面调用仍全绿）。
//
// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { StepReview } from './step-review';
import type { MappingItemDto } from '../lib/agent-md-flow';

afterEach(() => cleanup());

const ITEM: MappingItemDto = {
  id: 'e1',
  original_text: '张某某',
  entity_type: 'PERSON',
  replacement: '[人名_1]',
  excluded: false,
};

describe('StepReview 零实体闸（Issue#212）', () => {
  it('空映射：显示告警、确认按钮禁用，勾选知情后才可提交并带 ack', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(<StepReview items={[]} onConfirm={onConfirm} />);

    expect(screen.getByTestId('agent-md-zero-entities-warning')).toBeTruthy();
    const confirm = screen.getByTestId('agent-md-confirm') as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    // 未勾选前点击不应发请求
    fireEvent.click(confirm);
    expect(onConfirm).not.toHaveBeenCalled();

    fireEvent.click(screen.getByTestId('agent-md-zero-ack'));
    expect((screen.getByTestId('agent-md-confirm') as HTMLButtonElement).disabled).toBe(false);

    fireEvent.click(screen.getByTestId('agent-md-confirm'));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm.mock.calls[0][1]).toEqual({ acknowledgeZeroEntities: true });
  });

  it('有映射：不显示告警、按钮直接可用、提交不带 ack 标记', async () => {
    const onConfirm = vi.fn().mockResolvedValue(undefined);
    render(<StepReview items={[ITEM]} onConfirm={onConfirm} />);

    expect(screen.queryByTestId('agent-md-zero-entities-warning')).toBeNull();
    const confirm = screen.getByTestId('agent-md-confirm') as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);

    fireEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm.mock.calls[0][1]).toEqual({ acknowledgeZeroEntities: false });
  });
});