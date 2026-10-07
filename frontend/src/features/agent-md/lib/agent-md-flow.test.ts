import { describe, expect, it } from 'vitest';
import { buildDecisions, nextStepFromStatus } from './agent-md-flow';
import type { MappingItemDto } from './agent-md-flow';

describe('nextStepFromStatus', () => {
  it('maps backend states to flow steps', () => {
    expect(nextStepFromStatus('parsing')).toBe('processing');
    expect(nextStepFromStatus('ner_running')).toBe('processing');
    expect(nextStepFromStatus('mapping_ready')).toBe('review');
    expect(nextStepFromStatus('completed')).toBe('result');
    expect(nextStepFromStatus('failed')).toBe('processing');
  });
});

describe('buildDecisions', () => {
  const items: MappingItemDto[] = [
    { id: 'e1', original_text: '张三', entity_type: 'PERSON', replacement: '[人名_1]', excluded: false },
    { id: 'e2', original_text: '李四', entity_type: 'PERSON', replacement: '[人名_2]', excluded: false },
  ];

  it('emits only changed items', () => {
    const decisions = buildDecisions(items, { e1: { action: 'exclude' } });
    expect(decisions).toEqual([{ id: 'e1', action: 'exclude' }]);
  });

  it('custom replacement carries value', () => {
    const decisions = buildDecisions(items, { e2: { action: 'custom', replacement: '[人名_9]' } });
    expect(decisions).toEqual([{ id: 'e2', action: 'custom', replacement: '[人名_9]' }]);
  });

  it('empty edits yield empty decisions', () => {
    expect(buildDecisions(items, {})).toEqual([]);
  });

  it('decisions follow item order regardless of edit insertion order', () => {
    const decisions = buildDecisions(items, {
      e2: { action: 'keep' },
      e1: { action: 'exclude' },
    });
    expect(decisions).toEqual([
      { id: 'e1', action: 'exclude' },
      { id: 'e2', action: 'keep' },
    ]);
  });

  it('drops stale edits whose id is no longer in items', () => {
    const decisions = buildDecisions(items, {
      e1: { action: 'exclude' },
      ghost: { action: 'keep' },
    });
    expect(decisions).toEqual([{ id: 'e1', action: 'exclude' }]);
  });
});
