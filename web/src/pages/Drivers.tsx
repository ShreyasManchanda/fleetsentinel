import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { Badge, Button, Card, DataTable, Empty, ErrorState, Loading } from '../components/ui';

export default function Drivers() {
  const [cursorStack, setCursorStack] = useState<(string | undefined | null)[]>([undefined]);
  const cursor = cursorStack[cursorStack.length - 1] ?? undefined;
  const list = useQuery({ queryKey: ['drivers', cursor], queryFn: () => api.drivers(cursor, 20) });
  const [selected, setSelected] = useState<string | null>(null);
  const detail = useQuery({ queryKey: ['driver', selected], queryFn: () => api.driverDetail(selected!), enabled: Boolean(selected) });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-slate-900">Drivers</h1>
      <div className="grid gap-4 lg:grid-cols-2">
        <Card
          title="Scorecards"
          actions={
            <div className="flex gap-2">
              <Button variant="ghost" disabled={cursorStack.length <= 1} onClick={() => setCursorStack((s) => s.slice(0, -1))}>← Prev</Button>
              <Button variant="ghost" disabled={!list.data?.next_cursor} onClick={() => list.data?.next_cursor && setCursorStack((s) => [...s, list.data.next_cursor])}>Next →</Button>
            </div>
          }
        >
          {list.isLoading && <Loading label="Loading drivers…" />}
          {list.isError && <ErrorState error={list.error} onRetry={() => list.refetch()} />}
          {list.data && list.data.items.length === 0 && <Empty message="No drivers in this tenant." />}
          {list.data && list.data.items.length > 0 && (
            <DataTable
              caption="Driver scorecards"
              columns={[
                { header: 'Driver', render: (d) => d.display_name ?? <span className="text-slate-500">(pseudonymous)</span> },
                { header: 'Score', render: (d) => <span className="font-semibold">{d.safety_score.toFixed(0)}</span> },
                { header: 'Confidence', render: (d) => <Badge tone={d.confidence === 'ok' ? 'ok' : 'warn'}>{d.confidence}</Badge> },
                { header: 'km/30d', render: (d) => String(d.km_30d ?? '—') },
                { header: '', render: (d) => <Button variant="ghost" onClick={() => setSelected(d.id)}>Open</Button> },
              ]}
              rows={list.data.items}
              rowKey={(d) => d.id}
            />
          )}
        </Card>
        <Card title="Coaching detail">
          {!selected && <Empty message="Select a driver to see exposure-normalised detail and coaching levers." />}
          {selected && detail.isLoading && <Loading label="Loading driver…" />}
          {selected && detail.isError && <ErrorState error={detail.error} onRetry={() => detail.refetch()} />}
          {selected && detail.data && (
            <div className="space-y-3 text-sm">
              <p className="text-lg font-semibold text-slate-900">{detail.data.display_name ?? '(pseudonymous)'} — {detail.data.safety_score.toFixed(0)}</p>
              {detail.data.per_100km && (
                <dl className="grid grid-cols-2 gap-2">
                  {Object.entries(detail.data.per_100km).map(([k, v]) => (
                    <div key={k} className="rounded border border-slate-100 p-2">
                      <dt className="text-slate-600">{k.replace(/_/g, ' ')}</dt>
                      <dd className="font-semibold text-slate-900">{v}</dd>
                    </div>
                  ))}
                </dl>
              )}
              <h3 className="font-semibold text-slate-900">Coaching levers</h3>
              {(detail.data.coaching ?? []).length === 0 && <Empty message="No coaching levers — driving matches peers." />}
              <ul className="space-y-2">
                {(detail.data.coaching ?? []).map((c) => (
                  <li key={c.lever} className="rounded border border-amber-200 bg-amber-50 p-2">
                    <p className="font-medium text-amber-900">{c.lever}: {c.value} vs peer p50 {c.fleet_p50}</p>
                    <p className="text-amber-900">{c.message}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
