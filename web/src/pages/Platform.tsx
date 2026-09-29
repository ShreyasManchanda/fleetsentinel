import { useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { api } from '../api/client';
import { Badge, Button, Card, DataTable, Empty, ErrorState, Loading } from '../components/ui';

const SAMPLE_DOC = JSON.stringify({ oem: 'OEM_D', version: 3, mappings: [{ from: 'batt', to: 'battery.soc_pct' }] }, null, 2);
const SAMPLE_PAYLOAD = JSON.stringify([{ batt: 67, vin: 'MLHXXXXXXXXXXXXXX' }]);

export default function Platform() {
  const mappings = useQuery({ queryKey: ['mappings'], queryFn: () => api.mappings() });
  const quality = useQuery({ queryKey: ['oem-quality'], queryFn: () => api.oemQuality() });
  const dlq = useQuery({ queryKey: ['dlq'], queryFn: () => api.dlq() });
  const [doc, setDoc] = useState(SAMPLE_DOC);
  const [sample, setSample] = useState(SAMPLE_PAYLOAD);

  const preview = useMutation({
    mutationFn: () => {
      let d: unknown = {}; let s: unknown = [];
      try { d = JSON.parse(doc); } catch { throw new Error('Mapping document is not valid JSON'); }
      try { s = JSON.parse(sample); } catch { throw new Error('Sample payload is not valid JSON'); }
      return api.mappingPreview(d, s);
    },
  });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-slate-900">Platform</h1>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Mapping editor + live preview">
          <label htmlFor="mapping-doc" className="text-sm font-medium text-slate-800">Draft mapping document (JSON)</label>
          <textarea
            id="mapping-doc"
            rows={8}
            className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
            value={doc}
            onChange={(e) => setDoc(e.target.value)}
          />
          <label htmlFor="mapping-sample" className="mt-3 block text-sm font-medium text-slate-800">Sample payload (JSON)</label>
          <textarea
            id="mapping-sample"
            rows={3}
            className="mt-1 w-full rounded-md border border-slate-300 p-2 font-mono text-xs focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
            value={sample}
            onChange={(e) => setSample(e.target.value)}
          />
          <div className="mt-2">
            <Button onClick={() => preview.mutate()} disabled={preview.isPending}>
              {preview.isPending ? 'Previewing…' : 'Run preview'}
            </Button>
          </div>
          {preview.isError && <div className="mt-2"><ErrorState error={preview.error} onRetry={() => preview.mutate()} /></div>}
          {preview.data && (
            <div className="mt-3 text-sm">
              <p className="text-emerald-800">✓ {(preview.data.ok ?? []).length} records normalised</p>
              {(preview.data.failed ?? []).length > 0 && (
                <div className="mt-2">
                  <p className="font-semibold text-amber-900">{preview.data.failed!.length} failures</p>
                  <ul className="list-disc pl-5 text-slate-800">
                    {preview.data.failed!.map((f, i) => <li key={i}><span className="font-mono">{f.reason}</span> — {f.detail}</li>)}
                  </ul>
                </div>
              )}
              <pre className="mt-2 max-h-40 overflow-auto rounded bg-slate-900 p-2 text-xs text-slate-100">{JSON.stringify(preview.data.ok, null, 2)}</pre>
            </div>
          )}
        </Card>

        <Card title="Mapping versions">
          {mappings.isLoading && <Loading label="Loading mappings…" />}
          {mappings.isError && <ErrorState error={mappings.error} onRetry={() => mappings.refetch()} />}
          {mappings.data && (
            <DataTable
              caption="OEM mapping versions"
              columns={[
                { header: 'OEM', render: (m) => <span className="font-mono">{m.oem}</span> },
                { header: 'Version', render: (m) => String(m.version ?? '—') },
                {
                  header: 'Status',
                  render: (m) => <Badge tone={m.status === 'active' ? 'ok' : m.status === 'draft' ? 'warn' : 'muted'}>{m.status}</Badge>,
                },
              ]}
              rows={mappings.data}
              rowKey={(m) => `${m.oem}-v${m.version}`}
            />
          )}
        </Card>
      </div>

      <Card title="Data quality per OEM">
        {quality.isLoading && <Loading label="Loading quality…" />}
        {quality.isError && <ErrorState error={quality.error} onRetry={() => quality.refetch()} />}
        {quality.data && (
          <DataTable
            caption="OEM data quality"
            columns={[
              { header: 'OEM', render: (q) => <span className="font-mono">{q.oem}</span> },
              { header: 'Active map', render: (q) => String(q.active_mapping_version ?? 'none') },
              { header: 'Records/1h', render: (q) => String(q.records_1h ?? '—') },
              { header: 'Fail rate/1h', render: (q) => q.failure_rate_1h == null ? '—' : `${(q.failure_rate_1h * 100).toFixed(1)}%` },
              { header: 'Drift', render: (q) => q.drift_suspected ? <Badge tone="critical">drift</Badge> : <Badge tone="ok">clean</Badge> },
              { header: 'Score', render: (q) => String(q.quality_score ?? '—') },
            ]}
            rows={quality.data}
            rowKey={(q) => q.oem ?? Math.random().toString()}
          />
        )}
      </Card>

      <Card title="Dead-letter queue (sample)">
        {dlq.isLoading && <Loading label="Loading DLQ…" />}
        {dlq.isError && <ErrorState error={dlq.error} onRetry={() => dlq.refetch()} />}
        {dlq.data && dlq.data.length === 0 && <Empty message="DLQ is empty." />}
        {dlq.data && dlq.data.length > 0 && (
          <DataTable
            caption="DLQ sample"
            columns={[
              { header: 'OEM', render: (d) => <span className="font-mono">{d.oem}</span> },
              { header: 'Reason', render: (d) => <span className="font-mono">{d.error_reason}</span> },
              { header: 'Time', render: (d) => (d.ts ? new Date(d.ts).toLocaleString() : '—') },
              { header: 'Payload', render: (d) => <span className="font-mono text-xs">{JSON.stringify(d.payload)}</span> },
            ]}
            rows={dlq.data}
            rowKey={(_, i) => String(i)}
          />
        )}
      </Card>
    </div>
  );
}
