import { describe, expect, it } from 'vitest';
import { formatUSD, formatPct, formatAge } from '../lib/helpers';

describe('formatUSD', () => {
  it('formats whole dollars', () => {
    expect(formatUSD(18400)).toBe('$18,400');
  });
  it('renders em-dash for missing/NaN', () => {
    expect(formatUSD(undefined)).toBe('—');
    expect(formatUSD(null)).toBe('—');
    expect(formatUSD(NaN)).toBe('—');
  });
});

describe('formatPct', () => {
  it('formats fractions as percent', () => {
    expect(formatPct(0.723)).toBe('72.3%');
    expect(formatPct(0)).toBe('0.0%');
  });
  it('renders em-dash for missing', () => {
    expect(formatPct(undefined)).toBe('—');
  });
});

describe('formatAge', () => {
  const now = new Date('2026-09-29T12:00:00Z').getTime();
  it('formats seconds and minutes', () => {
    expect(formatAge('2026-09-29T11:59:30Z', now)).toBe('30s');
    expect(formatAge('2026-09-29T11:55:00Z', now)).toBe('5m 0s');
    expect(formatAge('2026-09-29T10:00:00Z', now)).toBe('2h 0m');
  });
  it('handles missing/invalid/future', () => {
    expect(formatAge(undefined, now)).toBe('—');
    expect(formatAge('not-a-date', now)).toBe('—');
    expect(formatAge('2026-09-29T12:05:00Z', now)).toBe('0s');
  });
});
