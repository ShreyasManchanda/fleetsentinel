import { describe, expect, it } from 'vitest';
import {
  aggregateToCells, ApiError, cellKey, decodeCursor, encodeCursor,
  filterViewport, formatAge, formatPct, formatUSD, mapProblemToError, nextCursor,
} from '../lib/helpers';

describe('cellKey precision', () => {
  it('respects the precision argument', () => {
    expect(cellKey(12.9, 77.5, 1.0)).toBe('12:77');
    expect(cellKey(12.9, 77.5)).toBe(cellKey(12.91, 77.51));
    expect(cellKey(12.9, 77.5, 1.0)).not.toBe(cellKey(13.9, 77.5, 1.0));
  });
  it('floors negative coordinates away from zero', () => {
    expect(cellKey(-0.1, -0.1)).toBe('-1:-1');
  });
});

describe('aggregateToCells edges', () => {
  it('counts only exact IDLING statuses as idle', () => {
    const cells = aggregateToCells([
      { vin: 'a', lat: 12.9, lon: 77.5, status: 'IDLING' },
      { vin: 'b', lat: 12.91, lon: 77.51, status: 'idling' },
      { vin: 'c', lat: 12.92, lon: 77.52, status: 'PARKED' },
    ]);
    expect(cells).toHaveLength(1);
    expect(cells[0].count).toBe(3);
    expect(cells[0].idle).toBe(1);
  });
  it('splits far-apart points into distinct cells', () => {
    const cells = aggregateToCells([
      { vin: 'a', lat: 12.9, lon: 77.5, status: 'DRIVING' },
      { vin: 'b', lat: 13.5, lon: 78.5, status: 'DRIVING' },
      { vin: 'c', lat: -33.8, lon: 151.2, status: 'DRIVING' },
    ]);
    expect(cells).toHaveLength(3);
    expect(cells.reduce((n, c) => n + c.count, 0)).toBe(3);
  });
});

describe('filterViewport edges', () => {
  const bbox: [number, number, number, number] = [77.4, 12.8, 77.8, 13.2];
  it('treats bbox edges as inclusive', () => {
    const pts = [
      { vin: 'w', lat: 13.0, lon: 77.4, status: 'DRIVING' },
      { vin: 'e', lat: 13.0, lon: 77.8, status: 'DRIVING' },
      { vin: 's', lat: 12.8, lon: 77.6, status: 'DRIVING' },
      { vin: 'n', lat: 13.2, lon: 77.6, status: 'DRIVING' },
      { vin: 'out', lat: 13.21, lon: 77.6, status: 'DRIVING' },
    ];
    const { visible, truncated } = filterViewport(pts, bbox, 2000);
    expect(visible.map((p) => p.vin).sort()).toEqual(['e', 'n', 's', 'w']);
    expect(truncated).toBe(false);
  });
  it('handles empty input and oversized caps', () => {
    expect(filterViewport([], bbox, 10)).toEqual({ visible: [], truncated: false });
    const one = [{ vin: 'a', lat: 13.0, lon: 77.6, status: 'DRIVING' }];
    const r = filterViewport(one, bbox, 100);
    expect(r.visible).toHaveLength(1);
    expect(r.truncated).toBe(false);
  });
});

describe('formatUSD edges', () => {
  it('formats zero, negatives, and rounds fractions', () => {
    expect(formatUSD(0)).toBe('$0');
    expect(formatUSD(-1200)).toBe('-$1,200');
    expect(formatUSD(18400.6)).toBe('$18,401');
    expect(formatUSD(1000000)).toBe('$1,000,000');
  });
});

describe('formatPct edges', () => {
  it('honours the digits argument and NaN', () => {
    expect(formatPct(0.7234, 2)).toBe('72.34%');
    expect(formatPct(0.7234, 0)).toBe('72%');
    expect(formatPct(NaN)).toBe('—');
    expect(formatPct(null)).toBe('—');
    expect(formatPct(-0.05)).toBe('-5.0%');
  });
});

describe('formatAge edges', () => {
  const now = new Date('2026-09-29T12:00:00Z').getTime();
  it('rolls minutes into hours with remainder', () => {
    expect(formatAge('2026-09-29T11:59:00Z', now)).toBe('1m 0s');
    expect(formatAge('2026-09-29T11:00:01Z', now)).toBe('59m 59s');
    expect(formatAge('2026-09-29T11:00:00Z', now)).toBe('1h 0m');
    expect(formatAge('2026-09-29T10:30:00Z', now)).toBe('1h 30m');
  });
});

describe('cursor edges', () => {
  it('round-trips zero and floors fractional offsets', () => {
    expect(decodeCursor(encodeCursor(0))).toBe(0);
    expect(decodeCursor(btoa('{"offset":1.9}'))).toBe(1);
    expect(decodeCursor(btoa('{}'))).toBe(0);
    expect(decodeCursor('')).toBe(0);
  });
  it('ends the list exactly at the boundary', () => {
    expect(nextCursor(0, 50, 51)).toBe(encodeCursor(50));
    expect(nextCursor(50, 50, 51)).toBe(null);
    expect(nextCursor(200, 50, 180)).toBe(null);
    expect(nextCursor(0, 50, 50)).toBe(null);
  });
});

describe('problem mapping completeness', () => {
  it('marks every retryable status and nothing else', () => {
    for (const s of [408, 429, 502, 503, 504]) {
      expect(mapProblemToError({ status: s, title: 't' }).retryable).toBe(true);
    }
    for (const s of [400, 401, 403, 404, 500]) {
      expect(mapProblemToError({ status: s, title: 't' }).retryable).toBe(false);
    }
  });
  it('defaults non-numeric status to 0 and keeps the detail', () => {
    const e = mapProblemToError({ status: '500' as unknown as number, title: '', detail: 'd' });
    expect(e.status).toBe(0);
    expect(e.title).toBe('Request failed');
    expect(e.detail).toBe('d');
    expect(e.retryable).toBe(false);
  });
});

describe('ApiError', () => {
  it('composes title + detail and preserves the shape', () => {
    const shape = { status: 403, title: 'Forbidden', detail: 'nope', retryable: false };
    const err = new ApiError(shape);
    expect(err).toBeInstanceOf(Error);
    expect(err.name).toBe('ApiError');
    expect(err.message).toBe('Forbidden: nope');
    expect(err.shape).toBe(shape);
  });
  it('falls back to the bare title without a detail', () => {
    const err = new ApiError({ status: 429, title: 'Too Many Requests', retryable: true });
    expect(err.message).toBe('Too Many Requests');
    expect(err.shape.traceId).toBeUndefined();
  });
});
