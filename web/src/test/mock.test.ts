import { describe, expect, it } from 'vitest';
import {
  BLR_BBOX, MOCK_ACTIONS, MOCK_ALERTS, MOCK_VEHICLES, mockApi,
} from '../api/mock';
import { AGGREGATION_ZOOM_THRESHOLD } from '../lib/helpers';

describe('mock pagination (keyset contract)', () => {
  it('walks the whole vehicle fleet ending on a null cursor', () => {
    let cursor: string | undefined;
    let total = 0;
    let pages = 0;
    for (;;) {
      const page = mockApi.vehicles(cursor, 400);
      total += page.items.length;
      pages += 1;
      if (page.next_cursor === null) break;
      cursor = page.next_cursor;
      expect(pages).toBeLessThan(10);
    }
    expect(total).toBe(MOCK_VEHICLES.length);
    expect(pages).toBe(3);
  });
  it('returns an empty last page consistently', () => {
    const full = mockApi.vehicles(undefined, MOCK_VEHICLES.length);
    expect(full.next_cursor).toBe(null);
    expect(full.items).toHaveLength(MOCK_VEHICLES.length);
  });
  it('filters alerts by VIN', () => {
    const vin = MOCK_ALERTS[0].vin;
    const page = mockApi.alerts(undefined, 50, vin);
    expect(page.items.length).toBeGreaterThan(0);
    expect(page.items.every((a) => a.vin === vin)).toBe(true);
  });
  it('paginates the audit log to a null cursor', () => {
    const p1 = mockApi.audit(undefined, 50);
    expect(p1.items).toHaveLength(50);
    expect(p1.next_cursor).not.toBe(null);
    const last = mockApi.audit(p1.next_cursor!, 500);
    expect(last.next_cursor).toBe(null);
  });
});

describe('mock map viewport (zoom contract)', () => {
  it('returns cells below the threshold and points at/above it', () => {
    const below = mockApi.mapViewport(BLR_BBOX, AGGREGATION_ZOOM_THRESHOLD - 1);
    expect(below.mode).toBe('cells');
    expect(below.cells!.length).toBeGreaterThan(0);
    expect(below.cells!.every((c) => c.count >= c.idle)).toBe(true);
    for (const z of [AGGREGATION_ZOOM_THRESHOLD, AGGREGATION_ZOOM_THRESHOLD + 3]) {
      const above = mockApi.mapViewport(BLR_BBOX, z);
      expect(above.mode).toBe('points');
      expect(above.points!.length).toBeGreaterThan(0);
      expect(above.points!.every((p) => p.vin && Number.isFinite(p.lat))).toBe(true);
    }
  });
  it('jitters deltas within a metre-scale bound and keeps identity', () => {
    const prev = MOCK_VEHICLES.slice(0, 20).map((v) => ({ vin: v.vin, lat: v.lat!, lon: v.lon! }));
    const next = mockApi.mapDeltas(prev);
    expect(next.map((p) => p.vin)).toEqual(prev.map((p) => p.vin));
    for (const [a, b] of prev.map((p, i) => [p, next[i]] as const)) {
      expect(Math.abs(a.lat - b.lat)).toBeLessThanOrEqual(0.001);
      expect(Math.abs(a.lon - b.lon)).toBeLessThanOrEqual(0.001);
    }
  });
});

describe('mock risk (threshold contract)', () => {
  it('applies the INSPECT/MONITOR/NONE thresholds and the value formula', () => {
    for (const v of MOCK_VEHICLES) {
      const r = mockApi.risk(v.vin);
      const p = v.last_risk_score ?? 0.2;
      expect(r.p_failure_7d).toBe(p);
      expect(r.recommendation).toBe(p > 0.7 ? 'INSPECT' : p > 0.35 ? 'MONITOR' : 'NONE');
      expect(r.expected_value_usd).toBe(Math.round(p * 4200 - 180));
      expect(r.model_version).toBe('lgbm-v3');
      expect(r.reason_codes).toHaveLength(3);
    }
  });
  it('covers all three recommendation bands', () => {
    const recs = new Set(MOCK_VEHICLES.map((v) => mockApi.risk(v.vin).recommendation));
    expect(recs).toEqual(new Set(['INSPECT', 'MONITOR', 'NONE']));
  });
});

describe('mock fleet summary and briefing', () => {
  it('keeps status counts consistent with the fleet total', () => {
    const s = mockApi.fleetSummary();
    const sum = Object.values(s.vehicles_by_status).reduce((a, b) => a + b, 0);
    expect(s.vehicles_total).toBe(sum);
    expect(s.open_critical_alerts).toBe(
      MOCK_ALERTS.filter((a) => a.severity === 'CRITICAL' && a.status === 'open').length,
    );
  });
  it('keeps briefing items ranked with a matching headline total', () => {
    const b = mockApi.briefing();
    const items = b.items!;
    expect(items.map((i) => i.rank)).toEqual([1, 2, 3, 4, 5]);
    const total = items.reduce((a, i) => a + (i.impact_usd ?? 0), 0);
    expect(b.total_impact_usd).toBe(total);
    const impacts = items.map((i) => i.impact_usd ?? 0);
    expect(impacts).toEqual([...impacts].sort((x, y) => y - x));
  });
});

describe('mock vehicle360 and drivers', () => {
  it('returns the requested vehicle with linked risk and trips', () => {
    const vin = MOCK_VEHICLES[7].vin;
    const v360 = mockApi.vehicle360(vin);
    expect(v360.vehicle.vin).toBe(vin);
    expect(v360.risk!.p_failure_7d).toBe(MOCK_VEHICLES[7].last_risk_score);
    expect(v360.recent_trips!.length).toBeGreaterThan(0);
  });
  it('exposes per-100km stats and coaching levers', () => {
    const d = mockApi.driverDetail('missing-id-falls-back');
    expect(d.per_100km).toBeDefined();
    expect(d.coaching!.length).toBeGreaterThan(0);
    expect(d.coaching!.every((c) => c.fleet_p50 !== undefined)).toBe(true);
  });
});

describe('mock approval queue transitions', () => {
  it('approves and rejects pending actions with timestamps', () => {
    const pending = MOCK_ACTIONS.filter((a) => a.status === 'pending');
    expect(pending.length).toBeGreaterThanOrEqual(2);
    const approved = mockApi.approveAction(pending[0].id);
    expect(approved.status).toBe('approved');
    expect(approved.work_order_id).not.toBe(null);
    expect(approved.decided_at).not.toBe(null);
    const rejected = mockApi.rejectAction(pending[1].id);
    expect(rejected.status).toBe('rejected');
    expect(rejected.decided_at).not.toBe(null);
    expect(mockApi.actions('pending').items.every((a) => a.status === 'pending')).toBe(true);
  });
});

describe('mock platform endpoints', () => {
  it('previews mappings with ok + failed buckets', () => {
    const r = mockApi.mappingPreview({ doc: 1 }, [{ a: 1 }, null]);
    expect(r.ok!.length).toBe(1);
    expect(r.failed!.length).toBe(1);
    expect(r.failed![0].reason).toBeTruthy();
  });
  it('flags the OEM_D drift in quality telemetry', () => {
    const q = mockApi.oemQuality();
    const d = q.find((x) => x.oem === 'OEM_D')!;
    expect(d.drift_suspected).toBe(true);
    expect(d.active_mapping_version).toBe(null);
    expect(q.filter((x) => x.oem !== 'OEM_D').every((x) => x.drift_suspected === false)).toBe(true);
  });
  it('creates DSRs as received with the requested kind', async () => {
    const dsr = mockApi.dsrCreate('driver-1', 'ERASE');
    expect(dsr.kind).toBe('ERASE');
    expect(dsr.status).toBe('received');
    expect(dsr.id).toBeTruthy();
  });
  it('exports insights with k-anonymity metadata', () => {
    const e = mockApi.insightExport();
    expect(e.k_anonymity).toBeGreaterThanOrEqual(2);
    expect(e.cells!.length).toBeGreaterThan(0);
    expect(e.suppressed_cells).toBeGreaterThanOrEqual(0);
  });
  it('verifies the mock audit chain as intact', () => {
    const v = mockApi.auditVerify();
    expect(v.ok).toBe(true);
    expect(v.checked).toBeGreaterThan(0);
    expect(v.first_bad_seq).toBe(null);
  });
});
