import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { formatUSD, formatPct } from '../lib/helpers';
import { Badge, Button, Card, DataTable, Empty, ErrorState, Loading } from '../components/ui';

function VinSearch() {
  const nav = useNavigate();
  const [vin, setVin] = useState('');
  return (
    <form
      className="flex gap-2"
      onSubmit={(e) => { e.preventDefault(); if (vin.trim()) nav(`/vehicles/${vin.trim().toUpperCase()}`); }}
      role="search"
      aria-label="Find vehicle by VIN"
    >
      <label htmlFor="vin-search" className="sr-only">VIN (17 characters)</label>
      <input
        id="vin-search"
        className="w-72 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
        placeholder="Enter 17-char VIN…"
        value={vin}
        onChange={(e) => setVin(e.target.value)}
        minLength={17}
        maxLength={17}
      />
      <Button type="submit">Open</Button>
    </form>
  );
}

export default function Vehicle360() {
  const { vin } = useParams<{ vin: string }>();
  const detail = useQuery({
    queryKey: ['vehicle-360', vin],
    queryFn: () => api.vehicle360(vin!),
    enabled: Boolean(vin),
  });
  const risk = useQuery({ queryKey: ['risk', vin], queryFn: () => api.risk(vin!), enabled: Boolean(vin) });
  const lookalikes = useQuery({ queryKey: ['lookalikes', vin], queryFn: () => api.lookalikes(vin!), enabled: Boolean(vin) });

  // Keyset pagination UI: cursor stack for back/forward.
  const [cursorStack, setCursorStack] = useState<(string | undefined | null)[]>([undefined]);
  const cursor = cursorStack[cursorStack.length - 1] ?? undefined;
  const trips = useQuery({
    queryKey: ['trips', vin, cursor],
    queryFn: () => api.trips(vin!, cursor, 10),
    enabled: Boolean(vin),
  });

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-xl font-bold text-slate-900">Vehicle 360</h1>
        <VinSearch />
      </div>
      {!vin && <Empty message="Enter a VIN above to open a vehicle." />}

      {vin && detail.isLoading && <Loading label="Loading vehicle…" />}
      {vin && detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} />}
      {vin && detail.data && (
        <>
          <Card title="Identity">
            <dl className="grid grid-cols-2 gap-2 text-sm lg:grid-cols-4">
              <div><dt className="text-slate-600">VIN</dt><dd className="font-mono font-semibold text-slate-900">{detail.data.vehicle.vin}</dd></div>
              <div><dt className="text-slate-600">Tenant / fleet</dt><dd className="text-slate-900">{detail.data.vehicle.fleet_id.slice(0, 8)}…</dd></div>
              <div><dt className="text-slate-600">OEM / model</dt><dd className="text-slate-900">{detail.data.vehicle.oem} · {detail.data.vehicle.model}</dd></div>
              <div><dt className="text-slate-600">Powertrain</dt><dd><Badge tone="info">{detail.data.vehicle.powertrain}</Badge></dd></div>
              <div><dt className="text-slate-600">Status</dt><dd><Badge tone={detail.data.vehicle.status === 'TOWED' ? 'critical' : 'muted'}>{detail.data.vehicle.status ?? '—'}</Badge></dd></div>
              <div><dt className="text-slate-600">Open alerts</dt><dd className="text-slate-900">{detail.data.vehicle.open_alerts ?? 0}</dd></div>
              <div><dt className="text-slate-600">Last risk</dt><dd className="text-slate-900">{formatPct(detail.data.vehicle.last_risk_score)}</dd></div>
              <div><dt className="text-slate-600">Location precision</dt><dd className="text-slate-900">{detail.data.location_precision ?? '—'}</dd></div>
            </dl>
          </Card>

          <div className="grid gap-4 lg:grid-cols-2">
            <Card title="Latest state (digital twin)">
              {detail.data.twin
                ? <pre className="overflow-x-auto rounded bg-slate-900 p-3 text-xs text-slate-100">{JSON.stringify(detail.data.twin, null, 2)}</pre>
                : <Empty message="No twin document." />}
            </Card>
            <Card title="Risk prediction + reason codes">
              {risk.isLoading && <Loading label="Loading risk…" />}
              {risk.isError && <ErrorState error={risk.error} onRetry={() => risk.refetch()} />}
              {risk.data && (
                <div className="space-y-2 text-sm">
                  <p className="text-2xl font-bold text-slate-900">{formatPct(risk.data.p_failure_7d)} <span className="text-sm font-normal text-slate-600">p(failure, 7d)</span></p>
                  <p className="text-slate-700">Recommendation: <Badge tone={risk.data.recommendation === 'INSPECT' ? 'critical' : risk.data.recommendation === 'MONITOR' ? 'warn' : 'ok'}>{risk.data.recommendation}</Badge>{' '}
                    <span className="text-slate-600">model {risk.data.model_version}{risk.data.fallback ? ' (rule fallback)' : ''}</span></p>
                  {risk.data.expected_value_usd != null && <p className="text-slate-700">Expected value: {formatUSD(risk.data.expected_value_usd)}</p>}
                  <ul className="list-disc pl-5 text-slate-800">
                    {(risk.data.reason_codes ?? []).map((r) => (
                      <li key={r.feature}><span className="font-mono">{r.feature}</span> (+{(r.contribution * 100).toFixed(0)}%) — {r.human}</li>
                    ))}
                  </ul>
                </div>
              )}
            </Card>
          </div>

          <Card title="Open alerts">
            {(detail.data.recent_alerts ?? []).length === 0
              ? <Empty message="No open alerts for this vehicle." />
              : (
                <DataTable
                  caption="Recent alerts"
                  columns={[
                    { header: 'Rule', render: (a) => <span className="font-mono">{a.rule}</span> },
                    { header: 'Severity', render: (a) => <Badge tone={a.severity === 'CRITICAL' ? 'critical' : a.severity === 'WARNING' ? 'warn' : 'info'}>{a.severity}</Badge> },
                    { header: 'Status', render: (a) => a.status },
                    { header: 'Opened', render: (a) => new Date(a.opened_at).toLocaleString() },
                    { header: 'Impact', render: (a) => formatUSD(a.impact_usd) },
                  ]}
                  rows={detail.data.recent_alerts ?? []}
                  rowKey={(a) => a.id}
                />
              )}
          </Card>

          <Card
            title="Trip history"
            actions={
              <div className="flex gap-2">
                <Button variant="ghost" disabled={cursorStack.length <= 1} onClick={() => setCursorStack((s) => s.slice(0, -1))}>← Newer</Button>
                <Button variant="ghost" disabled={!trips.data?.next_cursor} onClick={() => trips.data?.next_cursor && setCursorStack((s) => [...s, trips.data.next_cursor])}>Older →</Button>
              </div>
            }
          >
            {trips.isLoading && <Loading label="Loading trips…" />}
            {trips.isError && <ErrorState error={trips.error} onRetry={() => trips.refetch()} />}
            {trips.data && trips.data.items.length === 0 && <Empty message="No trips recorded." />}
            {trips.data && trips.data.items.length > 0 && (
              <DataTable
                caption="Trip history"
                columns={[
                  { header: 'Start', render: (t) => new Date(t.start_ts).toLocaleString() },
                  { header: 'Distance (km)', render: (t) => t.distance_km.toFixed(1) },
                  { header: 'Idle (s)', render: (t) => String(t.idle_s ?? '—') },
                  { header: 'Cost', render: (t) => formatUSD(t.energy_cost) },
                  { header: 'Harsh', render: (t) => `${t.harsh_brake_count ?? 0}B/${t.harsh_accel_count ?? 0}A` },
                ]}
                rows={trips.data.items}
                rowKey={(t) => t.id}
              />
            )}
          </Card>

          <Card title="Look-alike vehicles">
            {lookalikes.isLoading && <Loading label="Loading look-alikes…" />}
            {lookalikes.isError && <ErrorState error={lookalikes.error} onRetry={() => lookalikes.refetch()} />}
            {lookalikes.data && lookalikes.data.length === 0 && <Empty message="No look-alikes found." />}
            {lookalikes.data && lookalikes.data.length > 0 && (
              <DataTable
                caption="Look-alike vehicles"
                columns={[
                  { header: 'VIN', render: (l) => <span className="font-mono">{l.vin}</span> },
                  { header: 'Similarity', render: (l) => l.similarity.toFixed(2) },
                  { header: 'Failed ≤7d', render: (l) => l.failed_within_7d == null ? '—' : l.failed_within_7d ? 'yes' : 'no' },
                ]}
                rows={lookalikes.data}
                rowKey={(l) => l.vin}
              />
            )}
          </Card>
        </>
      )}
    </div>
  );
}
