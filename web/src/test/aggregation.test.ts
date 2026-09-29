import { describe, expect, it } from 'vitest';
import {
  aggregateToCells, cellKey, filterViewport, AGGREGATION_ZOOM_THRESHOLD,
  mapProblemToError,
} from '../lib/helpers';

describe('map aggregation', () => {
  it('buckets points into cells with idle counts', () => {
    const cells = aggregateToCells([
      { vin: 'a', lat: 12.9, lon: 77.5, status: 'IDLING' },
      { vin: 'b', lat: 12.91, lon: 77.51, status: 'DRIVING' },
      { vin: 'c', lat: 13.5, lon: 78.5, status: 'IDLING' },
    ]);
    expect(cells).toHaveLength(2);
    const home = cells.find((c) => c.geohash === cellKey(12.9, 77.5))!;
    expect(home.count).toBe(2);
    expect(home.idle).toBe(1);
  });
  it('empty input yields no cells', () => {
    expect(aggregateToCells([])).toEqual([]);
  });
  it('threshold constant matches the contract doc (zoom 9)', () => {
    expect(AGGREGATION_ZOOM_THRESHOLD).toBe(9);
  });
});

describe('viewport filter', () => {
  const pts = [
    { vin: 'a', lat: 13.0, lon: 77.6, status: 'DRIVING' },
    { vin: 'b', lat: 20.0, lon: 80.0, status: 'PARKED' },
  ];
  it('keeps only points inside the bbox', () => {
    const { visible, truncated } = filterViewport(pts, [77.4, 12.8, 77.8, 13.2], 2000);
    expect(visible.map((p) => p.vin)).toEqual(['a']);
    expect(truncated).toBe(false);
  });
  it('flags truncation at the cap', () => {
    const many = Array.from({ length: 10 }, (_, i) => ({ vin: String(i), lat: 13.0, lon: 77.6, status: 'DRIVING' }));
    const { visible, truncated } = filterViewport(many, [77.4, 12.8, 77.8, 13.2], 4);
    expect(visible).toHaveLength(4);
    expect(truncated).toBe(true);
  });
});

describe('problem mapping', () => {
  it('maps RFC 9457 fields and retryability', () => {
    const r429 = mapProblemToError({ status: 429, title: 'Too Many Requests', detail: 'slow down', trace_id: 'abc' });
    expect(r429.retryable).toBe(true);
    expect(r429.traceId).toBe('abc');
    const r403 = mapProblemToError({ status: 403, title: 'Forbidden' });
    expect(r403.retryable).toBe(false);
    const unknown = mapProblemToError({});
    expect(unknown.status).toBe(0);
    expect(unknown.title).toBe('Request failed');
  });
});
