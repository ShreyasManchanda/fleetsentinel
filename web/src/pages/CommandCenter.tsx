import { useEffect, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { Deck } from '@deck.gl/core';
import { ScatterplotLayer, TextLayer } from '@deck.gl/layers';
import maplibregl from 'maplibre-gl';
import 'maplibre-gl/dist/maplibre-gl.css';
import { api } from '../api/client';
import { mockApi, BLR_BBOX } from '../api/mock';
import { USE_MOCK } from '../api/client';
import { AGGREGATION_ZOOM_THRESHOLD } from '../lib/helpers';
import { formatUSD, formatAge } from '../lib/helpers';
import { Badge, Button, Card, Empty, ErrorState, Loading } from '../components/ui';
import type { MapViewport } from '../api/types';

const BLR_CENTER: [number, number] = [77.6, 13.0];
// Free demo tiles (no key). Self-hosted PMTiles Bengaluru basemap is NOT bundled (see report).
const MAP_STYLE = 'https://demotiles.maplibre.org/style.json';

const STATUS_COLORS: Record<string, [number, number, number, number]> = {
  DRIVING: [37, 99, 235, 200],
  PARKED: [100, 116, 139, 180],
  IDLING: [245, 158, 11, 210],
  CHARGING: [16, 185, 129, 210],
  OFFLINE: [75, 85, 99, 140],
  TOWED: [185, 28, 28, 220],
};

interface Pt { vin: string; lat: number; lon: number; status: string }

function ViewportMap({ viewport, deltas }: { viewport: MapViewport; deltas: Pt[] | null }) {
  const mapDiv = useRef<HTMLDivElement>(null);
  const deckCanvas = useRef<HTMLCanvasElement>(null);
  const deckRef = useRef<Deck | null>(null);
  const [mapError, setMapError] = useState<string | null>(null);
  const [zoom, setZoom] = useState(10);
  const zoomRef = useRef(10);

  useEffect(() => {
    if (!mapDiv.current || !deckCanvas.current) return;
    let map: maplibregl.Map | null = null;
    try {
      map = new maplibregl.Map({
        container: mapDiv.current,
        style: MAP_STYLE,
        center: BLR_CENTER,
        zoom: 10,
      });
      map.addControl(new maplibregl.NavigationControl(), 'top-right');
      const deck = new Deck({
        canvas: deckCanvas.current ?? undefined,
        initialViewState: { longitude: BLR_CENTER[0], latitude: BLR_CENTER[1], zoom: 10 },
        controller: false,
        layers: [],
      });
      deckRef.current = deck;
      map.on('move', () => {
        if (!map) return;
        const c = map.getCenter();
        const z = map.getZoom();
        zoomRef.current = z;
        setZoom(z);
        deck.setProps({
          viewState: {
            longitude: c.lng, latitude: c.lat,
            zoom: z, bearing: map.getBearing(), pitch: map.getPitch(),
          },
        });
      });
      map.on('error', (e) => setMapError(e.error?.message ?? 'Map failed to load (tiles need network). Points still listed below.'));
    } catch (e) {
      setMapError(e instanceof Error ? e.message : 'WebGL/map unavailable');
    }
    return () => {
      deckRef.current?.finalize();
      deckRef.current = null;
      map?.remove();
      map = null;
    };
  }, []);

  // Sync deck layers when data changes (points + aggr mode).
  useEffect(() => {
    const deck = deckRef.current;
    if (!deck) return;
    const layers = [];
    if (viewport.mode === 'cells' && viewport.cells) {
      layers.push(
        new ScatterplotLayer({
          id: 'cells',
          data: viewport.cells,
          getPosition: (c) => {
            // Mock cell key "i:j" -> decode to grid center for display.
            const [a, b] = String(c.geohash).split(':').map(Number);
            if (Number.isFinite(a) && Number.isFinite(b)) return [b * 0.5 + 0.25, a * 0.5 + 0.25];
            return [BLR_CENTER[0], BLR_CENTER[1]];
          },
          getRadius: (c) => Math.sqrt(c.count) * 220,
          getFillColor: [245, 158, 11, 160],
          stroked: true,
          getLineColor: [146, 64, 14],
          lineWidthMinPixels: 1,
          pickable: true,
        }),
        new TextLayer({
          id: 'cell-labels',
          data: viewport.cells,
          getPosition: (c) => {
            const [a, b] = String(c.geohash).split(':').map(Number);
            if (Number.isFinite(a) && Number.isFinite(b)) return [b * 0.5 + 0.25, a * 0.5 + 0.25];
            return [BLR_CENTER[0], BLR_CENTER[1]];
          },
          getText: (c) => String(c.count),
          getSize: 14,
          sizeUnits: 'pixels',
          getColor: [15, 23, 42],
        }),
      );
    } else if (viewport.points) {
      const pts: Pt[] = (deltas ?? viewport.points).map((p) => ({
        vin: (p as Pt).vin, lat: p.lat, lon: p.lon, status: (p as { status: string }).status,
      }));
      layers.push(
        new ScatterplotLayer({
          id: 'vehicles',
          data: pts,
          getPosition: (p: Pt) => [p.lon, p.lat],
          getFillColor: (p: Pt) => STATUS_COLORS[p.status] ?? [148, 163, 184, 180],
          getRadius: 60,
          radiusUnits: 'meters',
          radiusMinPixels: 2,
          radiusMaxPixels: 8,
          pickable: true,
        }),
      );
    }
    deck.setProps({ layers });
  }, [viewport, deltas]);

  const aggregated = zoom < AGGREGATION_ZOOM_THRESHOLD;
  return (
    <div>
      <div className="relative h-[420px] w-full overflow-hidden rounded-md border border-slate-200" role="application" aria-label="Live vehicle map">
        <div ref={mapDiv} className="absolute inset-0" />
        <canvas ref={deckCanvas} className="pointer-events-none absolute inset-0 h-full w-full" />
        <div className="absolute left-2 top-2">
          <Badge tone={aggregated ? 'warn' : 'info'}>
            {aggregated ? `Aggregated cells (zoom ${zoom.toFixed(1)} < 9)` : `Viewport points (zoom ${zoom.toFixed(1)})`}
          </Badge>
        </div>
      </div>
      {mapError && (
        <p role="alert" className="mt-2 text-sm text-amber-900">
          Basemap notice: {mapError} Deck.gl overlay data below remains available.
        </p>
      )}
    </div>
  );
}

export default function CommandCenter() {
  const summary = useQuery({ queryKey: ['fleet-summary'], queryFn: () => api.fleetSummary(), refetchInterval: 15000 });
  const [bbox] = useState<[number, number, number, number]>(BLR_BBOX);
  const [zoom] = useState(10); // REST snapshot zoom; live zoom shown on the map itself
  const viewportQuery = useQuery({
    queryKey: ['map-viewport', bbox.join(','), zoom],
    queryFn: () => api.mapViewport(bbox, zoom),
    // No per-second full refetch: snapshot only on bbox/zoom change.
    staleTime: 30000,
  });
  const alerts = useQuery({ queryKey: ['alerts-feed'], queryFn: () => api.alerts(undefined, 10), refetchInterval: 15000 });

  // 1 Hz deltas: client-side jitter of the snapshot points (mock mode).
  const [deltas, setDeltas] = useState<Pt[] | null>(null);
  useEffect(() => {
    if (viewportQuery.data?.mode === 'points' && viewportQuery.data.points) {
      setDeltas(viewportQuery.data.points.map((p) => ({ vin: p.vin, lat: p.lat, lon: p.lon, status: p.status })));
    } else {
      setDeltas(null);
    }
  }, [viewportQuery.data]);
  useEffect(() => {
    if (!USE_MOCK) return;
    const t = window.setInterval(() => {
      setDeltas((prev) => (prev ? mockApi.mapDeltas(prev).map((d, i) => ({ ...d, status: prev[i].status })) : prev));
    }, 1000);
    return () => window.clearInterval(t);
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-slate-900">Command Center</h1>

      {/* KPI strip */}
      {summary.isLoading && <Loading label="Loading fleet KPIs…" />}
      {summary.isError && <ErrorState error={summary.error} onRetry={() => summary.refetch()} />}
      {summary.data && (
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-5" role="region" aria-label="Fleet KPIs">
          <Card title="Open critical alerts">
            <p className="text-2xl font-bold text-red-800">{summary.data.open_critical_alerts}</p>
          </Card>
          <Card title="$ leaking today">
            <p className="text-2xl font-bold text-slate-900">{formatUSD(summary.data.leaking_usd_today)}</p>
          </Card>
          <Card title="At-risk vehicles">
            <p className="text-2xl font-bold text-slate-900">
              {summary.data.at_risk_vehicles}{' '}
              <span className="text-sm font-normal text-slate-600">
                ({((summary.data.at_risk_vehicles / Math.max(1, summary.data.vehicles_total)) * 100).toFixed(1)}%)
              </span>
            </p>
          </Card>
          <Card title="Savings opportunity / week">
            <p className="text-2xl font-bold text-emerald-800">{formatUSD(summary.data.savings_opportunity_usd_week)}</p>
          </Card>
          <Card title="Freshness">
            <p className="text-2xl font-bold text-slate-900" title={`as of ${summary.data.as_of}`}>
              {formatAge(summary.data.as_of)} <span className="text-sm font-normal text-slate-600">old</span>
            </p>
            <p className="text-xs text-slate-600">ingest p95 {summary.data.freshness_p95_ms} ms</p>
          </Card>
        </div>
      )}

      {/* Map */}
      <Card title="Live vehicle map — Bengaluru">
        {viewportQuery.isLoading && <Loading label="Loading map snapshot…" />}
        {viewportQuery.isError && <ErrorState error={viewportQuery.error} onRetry={() => viewportQuery.refetch()} />}
        {viewportQuery.data && <ViewportMap viewport={viewportQuery.data} deltas={deltas} />}
        {viewportQuery.data?.truncated && (
          <p className="mt-2 text-xs text-slate-600">Snapshot truncated to viewport (server-side aggregation at low zoom).</p>
        )}
      </Card>

      {/* Live alert feed */}
      <Card title="Live alert feed">
        {alerts.isLoading && <Loading label="Loading alerts…" />}
        {alerts.isError && <ErrorState error={alerts.error} onRetry={() => alerts.refetch()} />}
        {alerts.data && alerts.data.items.length === 0 && <Empty message="No alerts right now." />}
        {alerts.data && alerts.data.items.length > 0 && (
          <ul className="divide-y divide-slate-100">
            {alerts.data.items.map((a) => (
              <li key={a.id} className="flex items-center justify-between gap-3 py-2 text-sm">
                <div>
                  <Badge tone={a.severity === 'CRITICAL' ? 'critical' : a.severity === 'WARNING' ? 'warn' : 'info'}>{a.severity}</Badge>{' '}
                  <span className="font-mono text-slate-900">{a.rule}</span>{' '}
                  <a className="font-medium text-sky-800 underline" href={`/vehicles/${a.vin}`}>{a.vin.slice(-6)}</a>
                  <span className="text-slate-600"> · {formatAge(a.opened_at)} ago{a.latency_ms != null ? ` · ${a.latency_ms} ms` : ''}</span>
                </div>
                <Badge tone={a.status === 'open' ? 'critical' : 'muted'}>{a.status}</Badge>
              </li>
            ))}
          </ul>
        )}
        <div className="mt-3"><Button variant="ghost" onClick={() => alerts.refetch()}>Refresh feed</Button></div>
      </Card>
    </div>
  );
}
