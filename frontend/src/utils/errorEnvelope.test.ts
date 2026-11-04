// Copyright 2026 LexMask Contributors

import { describe, expect, it } from 'vitest';

import { errorEnvelopeMessage, firstMessageText } from './errorEnvelope';

describe('firstMessageText', () => {
  it('returns the first non-empty string among candidates', () => {
    expect(firstMessageText({}, 'message-text', 'error-text')).toBe('message-text');
    expect(firstMessageText(undefined, null, '', '  ', 'third')).toBe('third');
  });

  it('returns null when no candidate is a non-empty string', () => {
    expect(firstMessageText()).toBeNull();
    expect(firstMessageText({}, [], 42)).toBeNull();
  });

  it('trims the returned text', () => {
    expect(firstMessageText('  hello  ')).toBe('hello');
  });
});

describe('errorEnvelopeMessage (#232 M-3)', () => {
  const fallback = 'HTTP 400';

  it('prefers the first non-empty string field (detail→message→error)', () => {
    expect(errorEnvelopeMessage({ detail: 'specific reason', message: 'generic' }, fallback)).toBe(
      'specific reason',
    );
    expect(errorEnvelopeMessage({ detail: {}, message: 'generic' }, fallback)).toBe('generic');
    expect(errorEnvelopeMessage({ error: 'legacy' }, fallback)).toBe('legacy');
  });

  it('falls back instead of rendering "{}" for an empty object detail', () => {
    expect(errorEnvelopeMessage({ detail: {} }, fallback)).toBe(fallback);
    expect(errorEnvelopeMessage({}, fallback)).toBe(fallback);
    expect(errorEnvelopeMessage(null, fallback)).toBe(fallback);
  });

  it('keeps the stringify fallback for non-empty structured detail (e.g. 422 arrays)', () => {
    const detail = [{ loc: ['file'], msg: 'field required' }];
    expect(errorEnvelopeMessage({ detail }, fallback)).toBe(JSON.stringify(detail));
    expect(errorEnvelopeMessage({ detail: { missing: ['a.doc'] } }, fallback)).toBe(
      '{"missing":["a.doc"]}',
    );
  });
});
