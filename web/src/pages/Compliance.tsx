import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { Badge, Button, Card, DataTable, Empty, ErrorState, Loading } from '../components/ui';

export default function Compliance() {
  const [cursorStack, setCursorStack] = useState<(string | undefined | null)[]>([undefined]);
  const cursor = cursorStack[cursorStack.length - 1] ?? undefined;
  const audit = useQuery({ queryKey: ['audit', cursor], queryFn: () => api.audit(cursor, 20) });
  const [verifyResult, setVerifyResult] = useState<string | null>(null);
  const verify = useMutation({
    mutationFn: () => api.auditVerify(),
    onSuccess: (v) => setVerifyResult(v.ok ? `✓ Chain OK — ${v.checked} entries, head ${v.head_hash}` : `✗ BROKEN at seq ${v.first_bad_seq}`),
  });

  const [driverId, setDriverId] = useState('');
  const [kind, setKind] = useState<'ERASE' | 'ACCESS'>('ERASE');
  const dsr = useMutation({ mutationFn: () => api.dsrCreate(driverId.trim(), kind) });
  const insight = useQuery({ queryKey: ['insight-export'], queryFn: () => api.insightExport('2026-09-01', '2026-09-29'), enabled: false });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-slate-900">Compliance</h1>

      <Card title="Audit log" actions={<Button variant="ghost" onClick={() => verify.mutate()} disabled={verify.isPending}>{verify.isPending ? 'Verifying…' : 'Verify chain'}</Button>}>
        {verifyResult && <p role="status" className="mb-2 text-sm font-semibold text-slate-900">{verifyResult}</p>}
        {verify.isError && <ErrorState error={verify.error} />}
        {audit.isLoading && <Loading label="Loading audit…" />}
        {audit.isError && <ErrorState error={audit.error} onRetry={() => audit.refetch()} />}
        {audit.data && audit.data.items.length === 0 && <Empty message="No audit entries." />}
        {audit.data && audit.data.items.length > 0 && (
          <DataTable
            caption="Audit log"
            columns={[
              { header: 'Seq', render: (a) => String(a.seq) },
              { header: 'Time', render: (a) => new Date(a.ts).toLocaleString() },
              { header: 'Actor', render: (a) => a.actor_id },
              { header: 'Action', render: (a) => <span className="font-mono text-xs">{a.action}</span> },
              { header: 'Decision', render: (a) => <Badge tone={a.decision === 'ALLOW' ? 'ok' : 'critical'}>{a.decision}</Badge> },
              { header: 'Hash', render: (a) => <span className="font-mono text-xs">{a.hash.slice(0, 12)}…</span> },
            ]}
            rows={audit.data.items}
            rowKey={(a) => String(a.seq)}
          />
        )}
        <div className="mt-2 flex gap-2">
          <Button variant="ghost" disabled={cursorStack.length <= 1} onClick={() => setCursorStack((s) => s.slice(0, -1))}>← Prev</Button>
          <Button variant="ghost" disabled={!audit.data?.next_cursor} onClick={() => audit.data?.next_cursor && setCursorStack((s) => [...s, audit.data.next_cursor])}>Next →</Button>
        </div>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="DSR request (erasure / access)">
          <form
            className="space-y-2"
            onSubmit={(e) => { e.preventDefault(); if (driverId.trim()) dsr.mutate(); }}
          >
            <label htmlFor="dsr-driver" className="block text-sm font-medium text-slate-800">Driver ID (UUID)</label>
            <input
              id="dsr-driver"
              className="w-full rounded-md border border-slate-300 px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
              value={driverId}
              onChange={(e) => setDriverId(e.target.value)}
              placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
            />
            <div className="flex gap-2" role="group" aria-label="DSR kind">
              {(['ERASE', 'ACCESS'] as const).map((k) => (
                <Button key={k} type="button" variant={kind === k ? 'primary' : 'ghost'} onClick={() => setKind(k)}>{k}</Button>
              ))}
            </div>
            <Button type="submit" disabled={dsr.isPending || !driverId.trim()}>
              {dsr.isPending ? 'Submitting…' : 'Submit DSR'}
            </Button>
          </form>
          {dsr.isError && <div className="mt-2"><ErrorState error={dsr.error} /></div>}
          {dsr.data && (
            <p role="status" className="mt-2 text-sm text-slate-900">
              DSR {dsr.data.id} accepted — kind {dsr.data.kind}, status {dsr.data.status}.
            </p>
          )}
        </Card>

        <Card title="Privacy export (k-anonymity)" actions={<Button variant="ghost" onClick={() => insight.refetch()}>{insight.isFetching ? 'Loading…' : 'Load preview'}</Button>}>
          {insight.isError && <ErrorState error={insight.error} onRetry={() => insight.refetch()} />}
          {!insight.data && !insight.isFetching && <Empty message="Aggregates only: geohash-5 generalisation, cells with <10 vehicles suppressed. No VINs or driver identifiers." />}
          {insight.data && (
            <div className="text-sm text-slate-900">
              <p>k={insight.data.k_anonymity} · precision {insight.data.location_precision} · suppressed {insight.data.suppressed_cells} cells</p>
              <pre className="mt-2 max-h-48 overflow-auto rounded bg-slate-900 p-2 text-xs text-slate-100">{JSON.stringify(insight.data.cells, null, 2)}</pre>
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}
