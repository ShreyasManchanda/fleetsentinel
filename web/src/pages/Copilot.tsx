import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { formatUSD } from '../lib/helpers';
import { Badge, Button, Card, DataTable, Empty, ErrorState, Loading } from '../components/ui';

export default function Copilot() {
  const qc = useQueryClient();
  const [message, setMessage] = useState('Why is fleet leakage high today?');
  const [history, setHistory] = useState<{ role: 'user' | 'agent'; text: string; grounded?: boolean }[]>([]);
  const briefing = useQuery({ queryKey: ['briefing'], queryFn: () => api.briefing() });
  const [filter, setFilter] = useState<string>('pending');
  const actions = useQuery({ queryKey: ['actions', filter], queryFn: () => api.actions(filter) });

  const chat = useMutation({
    mutationFn: (msg: string) => api.chat(msg),
    onSuccess: (ans, msg) => {
      setHistory((h) => [...h, { role: 'user', text: msg }, { role: 'agent', text: ans.answer, grounded: ans.grounded }]);
      setMessage('');
    },
  });

  const decide = useMutation({
    mutationFn: ({ id, approve }: { id: string; approve: boolean }) =>
      approve ? api.approveAction(id) : api.rejectAction(id, 'Rejected from console'),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['actions'] }),
  });

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-bold text-slate-900">Copilot</h1>
      <div className="grid gap-4 lg:grid-cols-2">
        {/* Chat */}
        <Card title="Chat">
          <div aria-live="polite" className="mb-3 max-h-72 space-y-2 overflow-y-auto rounded border border-slate-100 p-2">
            {history.length === 0 && <Empty message="Ask about leakage, risk, or savings. Numbers are grounded in tool results." />}
            {history.map((m, i) => (
              <div key={i} className={`rounded p-2 text-sm ${m.role === 'user' ? 'bg-slate-100 text-slate-900' : 'bg-sky-50 text-slate-900'}`}>
                <p className="font-semibold">{m.role === 'user' ? 'You' : 'Copilot'}</p>
                <p>{m.text}</p>
                {m.role === 'agent' && (
                  <p className="mt-1 text-xs text-slate-600">{m.grounded ? '✓ grounded in tool results' : '⚠ ungrounded'}</p>
                )}
              </div>
            ))}
            {chat.isError && <ErrorState error={chat.error} />}
          </div>
          <form
            className="flex gap-2"
            onSubmit={(e) => { e.preventDefault(); if (message.trim()) chat.mutate(message.trim()); }}
          >
            <label htmlFor="copilot-input" className="sr-only">Message the copilot</label>
            <input
              id="copilot-input"
              className="flex-1 rounded-md border border-slate-300 px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600"
              value={message}
              onChange={(e) => setMessage(e.target.value)}
              maxLength={2000}
            />
            <Button type="submit" disabled={chat.isPending || !message.trim()}>
              {chat.isPending ? 'Sending…' : 'Send'}
            </Button>
          </form>
        </Card>

        {/* Briefing */}
        <Card title="Daily briefing" actions={<Button variant="ghost" onClick={() => briefing.refetch()}>Refresh</Button>}>
          {briefing.isLoading && <Loading label="Loading briefing…" />}
          {briefing.isError && <ErrorState error={briefing.error} onRetry={() => briefing.refetch()} />}
          {briefing.data && (
            <div className="text-sm">
              <p className="font-semibold text-slate-900">{briefing.data.headline}</p>
              <p className="text-slate-600">source: {briefing.data.source} · impact {formatUSD(briefing.data.total_impact_usd)}</p>
              <ul className="mt-2 space-y-2">
                {(briefing.data.items ?? []).map((it) => (
                  <li key={it.rank} className="rounded border border-slate-100 p-2">
                    <Badge tone={it.kind === 'maintenance' ? 'critical' : it.kind === 'cost' ? 'warn' : 'info'}>{it.kind}</Badge>{' '}
                    <span className="font-medium text-slate-900">{it.title}</span>
                    <p className="text-slate-600">{it.detail} · {formatUSD(it.impact_usd)}</p>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </Card>
      </div>

      {/* Approval queue */}
      <Card
        title="Approval queue"
        actions={
          <div className="flex gap-2" role="group" aria-label="Filter by status">
            {['pending', 'approved', 'rejected'].map((s) => (
              <Button key={s} variant={filter === s ? 'primary' : 'ghost'} onClick={() => setFilter(s)}>{s}</Button>
            ))}
          </div>
        }
      >
        {actions.isLoading && <Loading label="Loading proposals…" />}
        {actions.isError && <ErrorState error={actions.error} onRetry={() => actions.refetch()} />}
        {actions.data && actions.data.items.length === 0 && <Empty message={`No ${filter} proposals.`} />}
        {actions.data && actions.data.items.length > 0 && (
          <DataTable
            caption="Action proposals"
            columns={[
              { header: 'Proposal', render: (a) => <span className="font-mono text-xs">{JSON.stringify(a.proposal)}</span> },
              { header: 'Rationale', render: (a) => <span className="text-xs">{a.rationale}</span> },
              { header: 'Impact', render: (a) => formatUSD(a.expected_impact_usd) },
              { header: 'Status', render: (a) => <Badge tone={a.status === 'pending' ? 'warn' : a.status === 'approved' ? 'ok' : 'muted'}>{a.status}</Badge> },
              {
                header: 'Decision',
                render: (a) => a.status === 'pending' ? (
                  <div className="flex gap-1">
                    <Button disabled={decide.isPending} onClick={() => decide.mutate({ id: a.id, approve: true })}>Approve</Button>
                    <Button variant="danger" disabled={decide.isPending} onClick={() => decide.mutate({ id: a.id, approve: false })}>Reject</Button>
                  </div>
                ) : <span className="text-xs text-slate-600">{a.work_order_id ? `WO ${a.work_order_id.slice(0, 8)}` : '—'}</span>,
              },
            ]}
            rows={actions.data.items}
            rowKey={(a) => a.id}
          />
        )}
        {decide.isError && <div className="mt-2"><ErrorState error={decide.error} /></div>}
        <p className="mt-2 text-xs text-slate-600">Approve/reject POSTs carry an Idempotency-Key header (generated per click).</p>
      </Card>
    </div>
  );
}
