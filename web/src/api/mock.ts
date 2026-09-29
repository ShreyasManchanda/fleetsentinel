// Deterministic mock dataset matching contracts/openapi.yaml response schemas.
// Used when VITE_API_BASE_URL is unset (default dev mode). The mock implements
// the same typed interface as the real HTTP client so screens are agnostic.

import type {
  AgentAnswer, AgentAction, Alert, AuditEntry, Briefing, CostReport, DlqItem,
  Driver, DriverDetail, Dsr, FleetSummary, InsightExport, Lookalike, MapViewport,
  MappingPreviewResult, MappingSummary, Me, OemQuality, Page, RiskDetail,
  Trip, Vehicle, Vehicle360, VehicleStatus, AuditVerify,
} from './types';
import { aggregateToCells, decodeCursor, filterViewport, nextCursor, AGGREGATION_ZOOM_THRESHOLD } from '../lib/helpers';

// --- Seeded RNG (mulberry32) so the mock is stable across renders ---
function mulberry32(seed: number) {
  let a = seed >>> 0;
  return () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const rand = mulberry32(20260929);
const pick = <T,>(arr: T[]): T => arr[Math.floor(rand() * arr.length)];
const uuid = () => 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
  const r = Math.floor(rand() * 16);
  return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
});
const TENANT_ID = '11111111-1111-4111-8111-111111111111';
const nowIso = () => new Date().toISOString();
const minsAgo = (m: number) => new Date(Date.now() - m * 60000).toISOString();

// Bengaluru bounding box used across the console.
export const BLR_BBOX: [number, number, number, number] = [77.4, 12.8, 77.8, 13.2];

const OEMS = ['OEM_A', 'OEM_B', 'OEM_C', 'OEM_D'];
const MODELS = ['CityHaul 350', 'VoltTransit EV', 'MetroLiner Hybrid', 'CargoBolt EV'];
const STATUSES: VehicleStatus[] = ['PARKED', 'DRIVING', 'IDLING', 'CHARGING', 'OFFLINE', 'TOWED', 'DRIVING', 'DRIVING', 'PARKED', 'IDLING'];

function vinAt(i: number): string {
  const chars = 'ABCDEFGHJKLMNPRSTUVWXYZ0123456789';
  let s = 'MLH';
  let x = i * 2654435761 % 4294967296;
  for (let k = 0; k < 14; k++) { x = (x * 1103515245 + 12345) % 2147483648; s += chars[x % chars.length]; }
  return s.slice(0, 17);
}

// --- Fleet of 1,200 mock vehicles (UI sample of the 100k fleet) ---
export interface MockVehicle extends Vehicle { _idx: number }
export const MOCK_VEHICLES: MockVehicle[] = Array.from({ length: 1200 }, (_, i) => {
  const status = pick(STATUSES);
  const lat = 12.8 + rand() * 0.4;
  const lon = 77.4 + rand() * 0.4;
  return {
    _idx: i,
    vin: vinAt(i + 1),
    fleet_id: TENANT_ID,
    oem: pick(OEMS),
    model: pick(MODELS),
    powertrain: pick(['ICE', 'EV', 'HYBRID', 'EV', 'ICE'] as const),
    status,
    last_risk_score: Math.round(rand() * 100) / 100,
    open_alerts: rand() < 0.12 ? 1 + Math.floor(rand() * 3) : 0,
    lat, lon,
    last_seen: minsAgo(Math.floor(rand() * 15)),
  };
});

const RULES = ['TOW', 'CRASH', 'GEOFENCE_BREACH', 'CRITICAL_DTC', 'LOW_SOC_STRANDING', 'GOING_DARK', 'EXCESSIVE_IDLING', 'HARSH_DRIVING', 'MAINTENANCE_RISK', 'ODOMETER_ROLLBACK', 'SCHEMA_DRIFT'] as const;

export const MOCK_ALERTS: Alert[] = Array.from({ length: 180 }, (_, i) => {
  const v = MOCK_VEHICLES[Math.floor(rand() * MOCK_VEHICLES.length)];
  const sev = i % 9 === 0 ? 'CRITICAL' : i % 3 === 0 ? 'WARNING' : 'INFO';
  return {
    id: uuid(),
    vin: v.vin,
    rule: pick([...RULES]),
    severity: sev as Alert['severity'],
    status: i % 5 === 0 ? 'ack' : i % 11 === 0 ? 'resolved' : 'open',
    occurrences: 1 + Math.floor(rand() * 6),
    summary: `Mock alert #${i + 1} for ${v.vin.slice(-6)}`,
    impact_usd: Math.round(rand() * 900),
    opened_at: minsAgo(Math.floor(rand() * 600)),
    latency_ms: 800 + Math.floor(rand() * 3200),
    lat: v.lat, lon: v.lon,
    trace_id: uuid().slice(0, 16),
  };
});

export const MOCK_DRIVERS: (Driver & { _idx: number })[] = Array.from({ length: 120 }, (_, i) => ({
  _idx: i,
  id: uuid(),
  display_name: rand() < 0.9 ? `Driver ${String(i + 1).padStart(3, '0')}` : null,
  erased: false,
  safety_score: Math.round(40 + rand() * 58),
  confidence: rand() < 0.8 ? 'ok' : 'low',
  km_30d: Math.round(rand() * 4200),
}));

export const MOCK_ACTIONS: AgentAction[] = Array.from({ length: 12 }, (_, i) => ({
  id: uuid(),
  status: i < 6 ? 'pending' : i % 2 ? 'approved' : 'rejected',
  proposal: { type: i % 2 ? 'schedule_service' : 'coach_driver', vin: MOCK_VEHICLES[i].vin, by: '2026-10-02' },
  rationale: `Mock rationale ${i + 1}: expected breakdown cost exceeds inspection cost.`,
  expected_impact_usd: Math.round(500 + rand() * 4000),
  created_at: minsAgo(60 + i * 37),
  decided_at: i < 6 ? null : minsAgo(i * 11),
  work_order_id: i < 6 ? null : uuid(),
}));

export const MOCK_MAPPINGS: MappingSummary[] = OEMS.map((oem, i) => ({
  oem,
  version: oem === 'OEM_D' ? 2 : 5 - (i % 2),
  status: oem === 'OEM_D' ? 'draft' : 'active',
  created_by: 'platform_admin',
  created_at: minsAgo(60 * 24 * (i + 1)),
  activated_at: oem === 'OEM_D' ? null : minsAgo(60 * 24 * i + 60),
}));

export const MOCK_DLQ: DlqItem[] = Array.from({ length: 40 }, (_, i) => ({
  oem: i % 3 === 0 ? 'OEM_D' : pick(['OEM_A', 'OEM_B', 'OEM_C']),
  error_reason: pick(['unknown_field', 'type_mismatch', 'schema_drift', 'missing_vin']),
  ts: minsAgo(Math.floor(rand() * 300)),
  payload: { raw_speed: 'fast', batt: null as unknown, note: `mock dlq record ${i}` },
}));

export const MOCK_AUDIT: AuditEntry[] = Array.from({ length: 200 }, (_, i) => ({
  seq: i,
  ts: minsAgo(200 - i),
  actor_id: pick(['fleet_admin@demo', 'analyst@demo', 'copilot']),
  actor_type: 'user',
  action: pick(['vehicles.read', 'alerts.ack', 'agent.approve', 'mapping.activate', 'dsr.create']),
  resource_type: 'vehicle',
  resource_ids: [MOCK_VEHICLES[i % MOCK_VEHICLES.length].vin],
  purpose: 'OPERATIONS',
  decision: i % 17 === 0 ? 'DENY' : 'ALLOW',
  hash: `mockhash${String(i).padStart(4, '0')}`,
}));

function paginate<T>(all: T[], cursor: string | undefined, limit: number): Page<T> {
  const offset = decodeCursor(cursor);
  const items = all.slice(offset, offset + limit);
  return { items, next_cursor: nextCursor(offset, limit, all.length) };
}

// Mutable mock state for approvals/DSR so the UI feels live.
const actionState = new Map<string, AgentAction>(MOCK_ACTIONS.map((a) => [a.id, { ...a }]));
const dsrStore = new Map<string, Dsr>();

export const mockApi = {
  me(): Me {
    return { sub: 'dev-user', tenant_id: TENANT_ID, tenant_name: 'Demo Fleet', roles: ['fleet_admin'], email: 'demo@fleetsentinel.local' };
  },
  fleetSummary(): FleetSummary {
    const by: Record<string, number> = {};
    for (const v of MOCK_VEHICLES) by[v.status!] = (by[v.status!] ?? 0) + 1;
    return {
      vehicles_total: 100000,
      vehicles_by_status: { ...by, DRIVING: 41200, PARKED: 38800, IDLING: 8600, CHARGING: 5400, OFFLINE: 4900, TOWED: 1100 },
      leaking_usd_today: 18400,
      savings_opportunity_usd_week: 52300,
      at_risk_vehicles: MOCK_VEHICLES.filter((v) => (v.last_risk_score ?? 0) > 0.7).length * 83,
      open_critical_alerts: MOCK_ALERTS.filter((a) => a.severity === 'CRITICAL' && a.status === 'open').length,
      freshness_p95_ms: 1240,
      model_fallback_active: false,
      as_of: nowIso(),
    };
  },
  // Simulates GET /v1/map/viewport: cells below zoom 9, viewport points above.
  mapViewport(bbox: [number, number, number, number], zoom: number): MapViewport {
    const pts = MOCK_VEHICLES.map((v) => ({ vin: v.vin, lat: v.lat!, lon: v.lon!, status: v.status! }));
    if (zoom < AGGREGATION_ZOOM_THRESHOLD) {
      const cells = aggregateToCells(pts).map((c, i) => ({ geohash: `tdr1w${i}`, count: c.count * 83, idle: c.idle * 83 }));
      return { mode: 'cells', cells, truncated: true };
    }
    const { visible, truncated } = filterViewport(pts, bbox, 2000);
    return { mode: 'points', points: visible, truncated };
  },
  // 1 Hz deltas: small jitter applied client-side, no refetch.
  mapDeltas(prev: { vin: string; lat: number; lon: number }[]): { vin: string; lat: number; lon: number }[] {
    return prev.map((p) => ({
      vin: p.vin,
      lat: p.lat + (rand() - 0.5) * 0.002,
      lon: p.lon + (rand() - 0.5) * 0.002,
    }));
  },
  vehicles(cursor?: string, limit = 50): Page<Vehicle> {
    return paginate(MOCK_VEHICLES, cursor, limit);
  },
  vehicle360(vin: string): Vehicle360 {
    const vehicle = MOCK_VEHICLES.find((v) => v.vin === vin) ?? MOCK_VEHICLES[0];
    return {
      vehicle,
      twin: vehicle.powertrain === 'EV'
        ? { battery: { soc_pct: 67, soh_pct: 91, temp_c: 31 }, odometer_km: 48200 }
        : { fuel: { level_pct: 54 }, odometer_km: 96400, dtc: ['P0300'] },
      risk: this.risk(vin),
      recent_alerts: MOCK_ALERTS.filter((a) => a.vin === vin).slice(0, 5),
      recent_trips: this.trips(vin, undefined, 5).items,
      location_precision: 'exact',
    };
  },
  risk(vin: string): RiskDetail {
    const v = MOCK_VEHICLES.find((x) => x.vin === vin);
    const p = v?.last_risk_score ?? 0.2;
    return {
      p_failure_7d: p,
      model_version: 'lgbm-v3',
      fallback: false,
      reason_codes: [
        { feature: 'dtc_rate_7d', contribution: 0.31, human: 'Critical DTC rate above fleet p90' },
        { feature: 'idle_share_30d', contribution: 0.18, human: 'Idling share 2.1x peer median' },
        { feature: 'soc_min_7d', contribution: 0.12, human: 'Minimum SoC below 20% twice' },
      ],
      expected_value_usd: Math.round(p * 4200 - 180),
      recommendation: p > 0.7 ? 'INSPECT' : p > 0.35 ? 'MONITOR' : 'NONE',
      predicted_at: nowIso(),
    };
  },
  lookalikes(vin: string, k = 5): Lookalike[] {
    return MOCK_VEHICLES.filter((v) => v.vin !== vin).slice(0, k).map((v) => ({
      vin: v.vin,
      similarity: Math.round((0.7 + rand() * 0.29) * 100) / 100,
      failed_within_7d: rand() < 0.3,
    }));
  },
  trips(vin: string, cursor?: string, limit = 20): Page<Trip> {
    void vin;
    const all: Trip[] = Array.from({ length: 64 }, (_, i) => ({
      id: uuid(),
      start_ts: minsAgo(60 * 26 * (i + 1)),
      end_ts: minsAgo(60 * 26 * (i + 1) - 25),
      distance_km: Math.round(rand() * 1800) / 10,
      idle_s: Math.floor(rand() * 3600),
      energy_used: Math.round(rand() * 400) / 10,
      energy_cost: Math.round(rand() * 900) / 100,
      harsh_brake_count: Math.floor(rand() * 6),
      harsh_accel_count: Math.floor(rand() * 6),
      driver_ref: `drv_${(i % 9) + 1}`,
    }));
    return paginate(all, cursor, limit);
  },
  alerts(cursor?: string, limit = 50, vin?: string): Page<Alert> {
    const all = vin ? MOCK_ALERTS.filter((a) => a.vin === vin) : MOCK_ALERTS;
    return paginate(all, cursor, limit);
  },
  ackAlert(id: string): Alert {
    const a = MOCK_ALERTS.find((x) => x.id === id)!;
    a.status = 'ack';
    return { ...a };
  },
  resolveAlert(id: string): Alert {
    const a = MOCK_ALERTS.find((x) => x.id === id)!;
    a.status = 'resolved';
    return { ...a };
  },
  drivers(cursor?: string, limit = 50): Page<Driver> {
    return paginate(MOCK_DRIVERS, cursor, limit);
  },
  driverDetail(id: string): DriverDetail {
    const d = MOCK_DRIVERS.find((x) => x.id === id) ?? MOCK_DRIVERS[0];
    return {
      ...d,
      per_100km: { harsh_brake: 1.8, harsh_accel: 2.4, overspeed_min: 6.1, night_share: 0.22, idle_share: 0.14 },
      coaching: [
        { lever: 'harsh_braking', value: 1.8, fleet_p50: 0.9, message: '2.0x peer median — coach anticipation braking' },
        { lever: 'idling', value: 0.14, fleet_p50: 0.08, message: 'Idling 75% above peers — switch-off reminders' },
      ],
    };
  },
  costReport(): CostReport {
    return {
      currency: 'USD',
      total_cost: 48210,
      idle_cost: 18400,
      savings_opportunity: 52300,
      series: Array.from({ length: 7 }, (_, i) => ({
        key: `2026-09-${22 + i}`,
        energy_cost: Math.round(4000 + rand() * 3000),
        idle_cost: Math.round(1500 + rand() * 1200),
        distance_km: Math.round(80000 + rand() * 40000),
      })),
    };
  },
  briefing(): Briefing {
    return {
      generated_at: nowIso(),
      source: 'rules',
      headline: '5 actions could save $18,400 this week',
      total_impact_usd: 18400,
      items: [
        { rank: 1, kind: 'maintenance', title: 'Inspect 12 high-risk vehicles', detail: 'p_failure_7d above cost-optimal threshold', impact_usd: 9200 },
        { rank: 2, kind: 'cost', title: 'Cut depot idling at Peenya cluster', detail: '3.2x peer median idle share', impact_usd: 5400 },
        { rank: 3, kind: 'safety', title: 'Coach 8 harsh-braking drivers', detail: 'Top lever: anticipation braking', impact_usd: 2100 },
        { rank: 4, kind: 'ops', title: 'Review OEM_D drift', detail: 'Unknown format on 4% of records', impact_usd: 1100 },
        { rank: 5, kind: 'cost', title: 'Rebalance night charging', detail: 'Off-peak tariff opportunity', impact_usd: 600 },
      ],
    };
  },
  chat(message: string): AgentAnswer {
    void message;
    return {
      answer: 'Based on current fleet data: 20 open critical alerts and $18,400 leaking today (fleet summary). Top lever is depot idling at the Peenya cluster.',
      grounded: true,
      tool_calls: [{ tool: 'fleet.summary', args: {}, result_hash: 'mock9f2a' }],
      degraded: false,
    };
  },
  actions(status?: string): Page<AgentAction> {
    const all = [...actionState.values()].filter((a) => !status || status === 'all' || a.status === status);
    return { items: all, next_cursor: null };
  },
  approveAction(id: string): AgentAction {
    const a = actionState.get(id)!;
    a.status = 'approved';
    a.decided_at = nowIso();
    a.work_order_id = uuid();
    return { ...a };
  },
  rejectAction(id: string): AgentAction {
    const a = actionState.get(id)!;
    a.status = 'rejected';
    a.decided_at = nowIso();
    return { ...a };
  },
  mappings(): MappingSummary[] {
    return MOCK_MAPPINGS;
  },
  mappingPreview(doc: unknown, sample: unknown): MappingPreviewResult {
    void doc;
    const rows = Array.isArray(sample) ? sample : [sample];
    return {
      ok: rows.filter(Boolean).map((r) => ({ normalised: true, input_keys: r && typeof r === 'object' ? Object.keys(r as object) : [] })),
      failed: [{ reason: 'unknown_field', detail: 'mock: batt is null in 1 record', raw: rows[0] }],
    };
  },
  oemQuality(): OemQuality[] {
    return OEMS.map((oem) => ({
      oem,
      active_mapping_version: oem === 'OEM_D' ? null : 4,
      records_1h: Math.floor(20000 + rand() * 80000),
      failure_rate_1h: oem === 'OEM_D' ? 0.42 : rand() * 0.02,
      drift_suspected: oem === 'OEM_D',
      drift_detail: oem === 'OEM_D' ? 'Columnar format detected mid-run (OEM_D schema-drift event)' : '',
      quality_score: oem === 'OEM_D' ? 58 : Math.round(90 + rand() * 9),
      fault_rate_by_sw_version: [{ sw_version: 'v2.4.1', vehicles: 1200, dtc_per_1000km: 1.2 }],
    }));
  },
  dlq(): DlqItem[] {
    return MOCK_DLQ;
  },
  audit(cursor?: string, limit = 50): Page<AuditEntry> {
    return paginate(MOCK_AUDIT, cursor, limit);
  },
  auditVerify(): AuditVerify {
    return { ok: true, checked: MOCK_AUDIT.length, first_bad_seq: null, head_hash: 'mockheadhash', anchored_day: '2026-09-28' };
  },
  dsrCreate(driver_id: string, kind: 'ERASE' | 'ACCESS'): Dsr {
    const dsr: Dsr = { id: uuid(), kind, status: 'received', requested_at: nowIso(), completed_at: null, evidence: {} };
    dsrStore.set(dsr.id, dsr);
    void driver_id;
    return dsr;
  },
  insightExport(): InsightExport {
    return {
      k_anonymity: 10,
      location_precision: 'geohash5',
      cells: Array.from({ length: 12 }, (_, i) => ({
        geohash: `tdr1w${i}`,
        vehicles_approx: 40 + i * 12,
        idle_hours: Math.round(rand() * 90),
        harsh_events: Math.floor(rand() * 60),
      })),
      suppressed_cells: 3,
      totals: { vehicles_approx: 984, idle_hours: 512 },
    };
  },
};
