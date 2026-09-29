// Pure helpers: formatters, keyset cursors, map aggregation, error mapping.
// All covered by Vitest (see src/test/*.test.ts).

export function formatUSD(n: number | undefined | null): string {
  if (n === undefined || n === null || Number.isNaN(n)) return '—';
  return n.toLocaleString('en-US', { style: 'currency', currency: 'USD', maximumFractionDigits: 0 });
}

export function formatPct(n: number | undefined | null, digits = 1): string {
  if (n === undefined || n === null || Number.isNaN(n)) return '—';
  return `${(n * 100).toFixed(digits)}%`;
}

export function formatAge(isoTs: string | undefined, nowMs = Date.now()): string {
  if (!isoTs) return '—';
  const ms = nowMs - new Date(isoTs).getTime();
  if (Number.isNaN(ms)) return '—';
  if (ms < 0) return '0s';
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

// --- Opaque keyset cursors (base64 of JSON {offset}) ---
// The server owns cursor opacity; the client only round-trips the string.
export function encodeCursor(offset: number): string {
  return btoa(JSON.stringify({ offset }));
}

export function decodeCursor(cursor: string | null | undefined): number {
  if (!cursor) return 0;
  try {
    const parsed = JSON.parse(atob(cursor)) as { offset?: unknown };
    return typeof parsed.offset === 'number' && parsed.offset >= 0 ? Math.floor(parsed.offset) : 0;
  } catch {
    return 0;
  }
}

export function nextCursor(offset: number, limit: number, total: number): string | null {
  return offset + limit < total ? encodeCursor(offset + limit) : null;
}

// --- Viewport aggregation ---
// Zoom < 9 -> geohash-cell counts; zoom >= 9 -> individual points (viewport only).
export const AGGREGATION_ZOOM_THRESHOLD = 9;

export interface RawPoint {
  vin: string;
  lat: number;
  lon: number;
  status: string;
}

export interface Cell {
  geohash: string;
  count: number;
  idle: number;
}

// Coarse cell key (~0.5deg grid, stands in for geohash at low zoom).
export function cellKey(lat: number, lon: number, precision = 0.5): string {
  return `${Math.floor(lat / precision)}:${Math.floor(lon / precision)}`;
}

export function aggregateToCells(points: RawPoint[]): Cell[] {
  const map = new Map<string, { count: number; idle: number }>();
  for (const p of points) {
    const key = cellKey(p.lat, p.lon);
    const entry = map.get(key) ?? { count: 0, idle: 0 };
    entry.count += 1;
    if (p.status === 'IDLING') entry.idle += 1;
    map.set(key, entry);
  }
  return [...map.entries()].map(([geohash, v]) => ({ geohash, ...v }));
}

export function filterViewport<T extends RawPoint>(
  points: T[],
  bbox: [number, number, number, number],
  maxPoints: number,
): { visible: T[]; truncated: boolean } {
  const [w, s, e, n] = bbox;
  const visible: T[] = [];
  for (const p of points) {
    if (p.lon >= w && p.lon <= e && p.lat >= s && p.lat <= n) {
      visible.push(p);
      if (visible.length >= maxPoints) return { visible, truncated: true };
    }
  }
  return { visible, truncated: false };
}

// --- RFC 9457 problem mapping ---
export interface ApiErrorShape {
  status: number;
  title: string;
  detail?: string;
  traceId?: string;
  retryable: boolean;
}

const RETRYABLE = new Set([408, 429, 502, 503, 504]);

export function mapProblemToError(problem: {
  status?: number;
  title?: string;
  detail?: string;
  trace_id?: string;
}): ApiErrorShape {
  const status = typeof problem.status === 'number' ? problem.status : 0;
  return {
    status,
    title: problem.title || 'Request failed',
    detail: problem.detail,
    traceId: problem.trace_id,
    retryable: RETRYABLE.has(status),
  };
}

export class ApiError extends Error {
  readonly shape: ApiErrorShape;
  constructor(shape: ApiErrorShape) {
    super(shape.detail ? `${shape.title}: ${shape.detail}` : shape.title);
    this.name = 'ApiError';
    this.shape = shape;
  }
}
