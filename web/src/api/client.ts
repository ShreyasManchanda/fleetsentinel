// Single typed API surface. Mock-first: when VITE_API_BASE_URL is unset every
// screen runs against the in-memory mock (same shapes as openapi.yaml).
// Set VITE_API_BASE_URL=http://localhost:8000 to hit the real API — no screen
// code changes, only this module branches.

import { ApiError, mapProblemToError } from '../lib/helpers';
import { mockApi } from './mock';
import type {
  AgentAction, Alert, AuditEntry, Briefing, CostReport, DlqItem, Driver,
  DriverDetail, Dsr, FleetSummary, InsightExport, Lookalike, MapViewport,
  MappingPreviewResult, MappingSummary, Me, OemQuality, Page, Purpose,
  RiskDetail, Trip, Vehicle, Vehicle360, AuditVerify, AgentAnswer,
} from './types';

const BASE = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, '') ?? '';
export const USE_MOCK = BASE === '';
export const API_BASE = USE_MOCK ? '(mock)' : BASE;

let token: string | null = localStorage.getItem('fs_token');
export function setToken(t: string | null) {
  token = t;
  if (t) localStorage.setItem('fs_token', t);
  else localStorage.removeItem('fs_token');
}
export function getToken() { return token; }

export function idempotencyKey(): string {
  return crypto.randomUUID();
}

async function http<T>(path: string, init: RequestInit = {}, purpose?: Purpose): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  if (token) headers.Authorization = `Bearer ${token}`;
  if (purpose) headers['X-Purpose'] = purpose;
  const res = await fetch(`${BASE}${path}`, { ...init, headers });
  const contentType = res.headers.get('content-type') ?? '';
  if (!res.ok) {
    if (contentType.includes('application/problem+json') || contentType.includes('json')) {
      const body = await res.json().catch(() => ({}));
      throw new ApiError(mapProblemToError(body));
    }
    throw new ApiError(mapProblemToError({ status: res.status, title: res.statusText || 'Request failed' }));
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

const get = <T,>(path: string, purpose?: Purpose) => http<T>(path, { method: 'GET' }, purpose);
const post = <T,>(path: string, body?: unknown, opts?: { idempotencyKey?: string; purpose?: Purpose }) =>
  http<T>(path, {
    method: 'POST',
    body: body === undefined ? undefined : JSON.stringify(body),
    headers: opts?.idempotencyKey ? { 'Idempotency-Key': opts.idempotencyKey } : {},
  }, opts?.purpose);

function page(path: string, cursor?: string, limit = 50): string {
  const q = new URLSearchParams();
  if (cursor) q.set('cursor', cursor);
  q.set('limit', String(limit));
  return `${path}?${q}`;
}

export const api = {
  me: (): Promise<Me> => (USE_MOCK ? Promise.resolve(mockApi.me()) : get<Me>('/v1/me')),
  fleetSummary: (): Promise<FleetSummary> =>
    USE_MOCK ? Promise.resolve(mockApi.fleetSummary()) : get<FleetSummary>('/v1/fleet/summary'),
  mapViewport: (bbox: [number, number, number, number], zoom: number, purpose: Purpose = 'OPERATIONS'): Promise<MapViewport> =>
    USE_MOCK
      ? Promise.resolve(mockApi.mapViewport(bbox, zoom))
      : get<MapViewport>(`/v1/map/viewport?bbox=${bbox.join(',')}&zoom=${zoom}`, purpose),
  vehicles: (cursor?: string, limit = 50): Promise<Page<Vehicle>> =>
    USE_MOCK ? Promise.resolve(mockApi.vehicles(cursor, limit)) : get<Page<Vehicle>>(page('/v1/vehicles', cursor, limit)),
  vehicle360: (vin: string): Promise<Vehicle360> =>
    USE_MOCK ? Promise.resolve(mockApi.vehicle360(vin)) : get<Vehicle360>(`/v1/vehicles/${vin}`, 'OPERATIONS'),
  risk: (vin: string): Promise<RiskDetail> =>
    USE_MOCK ? Promise.resolve(mockApi.risk(vin)) : get<RiskDetail>(`/v1/vehicles/${vin}/risk`),
  lookalikes: (vin: string, k = 5): Promise<Lookalike[]> =>
    USE_MOCK ? Promise.resolve(mockApi.lookalikes(vin, k)) : get<Lookalike[]>(`/v1/vehicles/${vin}/lookalikes?k=${k}`),
  trips: (vin: string, cursor?: string, limit = 20): Promise<Page<Trip>> =>
    USE_MOCK ? Promise.resolve(mockApi.trips(vin, cursor, limit)) : get<Page<Trip>>(page(`/v1/vehicles/${vin}/trips`, cursor, limit)),
  alerts: (cursor?: string, limit = 50, vin?: string): Promise<Page<Alert>> =>
    USE_MOCK
      ? Promise.resolve(mockApi.alerts(cursor, limit, vin))
      : get<Page<Alert>>(page('/v1/alerts', cursor, limit) + (vin ? `&vin=${encodeURIComponent(vin)}` : '')),
  ackAlert: (id: string): Promise<Alert> =>
    USE_MOCK ? Promise.resolve(mockApi.ackAlert(id)) : post<Alert>(`/v1/alerts/${id}/ack`, undefined, { idempotencyKey: idempotencyKey() }),
  resolveAlert: (id: string): Promise<Alert> =>
    USE_MOCK ? Promise.resolve(mockApi.resolveAlert(id)) : post<Alert>(`/v1/alerts/${id}/resolve`, undefined, { idempotencyKey: idempotencyKey() }),
  drivers: (cursor?: string, limit = 50): Promise<Page<Driver>> =>
    USE_MOCK ? Promise.resolve(mockApi.drivers(cursor, limit)) : get<Page<Driver>>(page('/v1/drivers', cursor, limit)),
  driverDetail: (id: string): Promise<DriverDetail> =>
    USE_MOCK ? Promise.resolve(mockApi.driverDetail(id)) : get<DriverDetail>(`/v1/drivers/${id}`),
  costReport: (from: string, to: string): Promise<CostReport> =>
    USE_MOCK ? Promise.resolve(mockApi.costReport()) : get<CostReport>(`/v1/reports/cost?from=${from}&to=${to}`),
  chat: (message: string, conversation_id?: string): Promise<AgentAnswer> =>
    USE_MOCK ? Promise.resolve(mockApi.chat(message)) : post<AgentAnswer>('/v1/agent/chat', { message, conversation_id }),
  briefing: (): Promise<Briefing> =>
    USE_MOCK ? Promise.resolve(mockApi.briefing()) : get<Briefing>('/v1/agent/briefing'),
  actions: (status?: string): Promise<Page<AgentAction>> =>
    USE_MOCK
      ? Promise.resolve(mockApi.actions(status))
      : get<Page<AgentAction>>(`/v1/agent/actions${status ? `?status=${status}` : ''}`),
  approveAction: (id: string): Promise<AgentAction> =>
    USE_MOCK ? Promise.resolve(mockApi.approveAction(id)) : post<AgentAction>(`/v1/agent/actions/${id}/approve`, undefined, { idempotencyKey: idempotencyKey() }),
  rejectAction: (id: string, reason?: string): Promise<AgentAction> =>
    USE_MOCK ? Promise.resolve(mockApi.rejectAction(id)) : post<AgentAction>(`/v1/agent/actions/${id}/reject`, { reason }, { idempotencyKey: idempotencyKey() }),
  mappings: (): Promise<MappingSummary[]> =>
    USE_MOCK ? Promise.resolve(mockApi.mappings()) : get<MappingSummary[]>('/v1/mappings'),
  mappingPreview: (document: unknown, sample: unknown): Promise<MappingPreviewResult> =>
    USE_MOCK
      ? Promise.resolve(mockApi.mappingPreview(document, sample))
      : post<MappingPreviewResult>('/v1/mappings/preview', { document, sample }),
  oemQuality: (): Promise<OemQuality[]> =>
    USE_MOCK ? Promise.resolve(mockApi.oemQuality()) : get<OemQuality[]>('/v1/oems/quality'),
  dlq: (): Promise<DlqItem[]> =>
    USE_MOCK ? Promise.resolve(mockApi.dlq()) : get<DlqItem[]>('/v1/dlq?limit=50'),
  audit: (cursor?: string, limit = 50): Promise<Page<AuditEntry>> =>
    USE_MOCK ? Promise.resolve(mockApi.audit(cursor, limit)) : get<Page<AuditEntry>>(page('/v1/audit', cursor, limit)),
  auditVerify: (): Promise<AuditVerify> =>
    USE_MOCK ? Promise.resolve(mockApi.auditVerify()) : get<AuditVerify>('/v1/audit/verify'),
  dsrCreate: (driver_id: string, kind: 'ERASE' | 'ACCESS'): Promise<Dsr> =>
    USE_MOCK ? Promise.resolve(mockApi.dsrCreate(driver_id, kind)) : post<Dsr>('/v1/dsr', { driver_id, kind }, { idempotencyKey: idempotencyKey() }),
  insightExport: (from: string, to: string): Promise<InsightExport> =>
    USE_MOCK ? Promise.resolve(mockApi.insightExport()) : get<InsightExport>(`/v1/insights/export?from=${from}&to=${to}`),
};
