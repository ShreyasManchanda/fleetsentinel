import { ApiError } from '../lib/helpers';

// Shared accessible UI primitives (Tailwind only; shadcn skipped — see report).

export function Badge({ tone, children }: { tone: 'critical' | 'warn' | 'ok' | 'info' | 'muted'; children: React.ReactNode }) {
  const tones: Record<string, string> = {
    critical: 'bg-red-100 text-red-900 border-red-300',
    warn: 'bg-amber-100 text-amber-900 border-amber-300',
    ok: 'bg-emerald-100 text-emerald-900 border-emerald-300',
    info: 'bg-sky-100 text-sky-900 border-sky-300',
    muted: 'bg-slate-100 text-slate-800 border-slate-300',
  };
  return (
    <span className={`inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-semibold ${tones[tone]}`}>
      {children}
    </span>
  );
}

export function Button(props: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: 'primary' | 'ghost' | 'danger' }) {
  const { variant = 'primary', className = '', ...rest } = props;
  const styles =
    variant === 'primary'
      ? 'bg-slate-900 text-white hover:bg-slate-700 focus-visible:ring-slate-900'
      : variant === 'danger'
        ? 'bg-red-700 text-white hover:bg-red-600 focus-visible:ring-red-700'
        : 'bg-white text-slate-900 border border-slate-300 hover:bg-slate-100 focus-visible:ring-slate-500';
  return (
    <button
      {...rest}
      className={`rounded-md px-3 py-1.5 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 disabled:opacity-50 ${styles} ${className}`}
    />
  );
}

export function Card({ title, children, actions }: { title?: string; children: React.ReactNode; actions?: React.ReactNode }) {
  return (
    <section aria-label={title} className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
      {(title || actions) && (
        <div className="mb-3 flex items-center justify-between gap-2">
          {title && <h2 className="text-base font-semibold text-slate-900">{title}</h2>}
          {actions}
        </div>
      )}
      {children}
    </section>
  );
}

export function Loading({ label = 'Loading…' }: { label?: string }) {
  return (
    <div role="status" aria-live="polite" className="flex items-center gap-2 p-4 text-sm text-slate-700">
      <span aria-hidden="true" className="inline-block h-4 w-4 animate-spin rounded-full border-2 border-slate-400 border-t-transparent" />
      {label}
    </div>
  );
}

export function Empty({ message, action }: { message: string; action?: React.ReactNode }) {
  return (
    <div className="rounded-md border border-dashed border-slate-300 p-6 text-center text-sm text-slate-700">
      <p>{message}</p>
      {action && <div className="mt-3">{action}</div>}
    </div>
  );
}

export function ErrorState({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const detail =
    error instanceof ApiError
      ? `${error.shape.title}${error.shape.detail ? ` — ${error.shape.detail}` : ''} (HTTP ${error.shape.status})`
      : error instanceof Error
        ? error.message
        : 'Unknown error';
  return (
    <div role="alert" className="rounded-md border border-red-300 bg-red-50 p-4 text-sm text-red-900">
      <p className="font-semibold">Something went wrong</p>
      <p className="mt-1">{detail}</p>
      {onRetry && (
        <div className="mt-3">
          <Button variant="ghost" onClick={onRetry}>Retry</Button>
        </div>
      )}
    </div>
  );
}

export function DataTable<T>({ columns, rows, rowKey, caption }: {
  columns: { header: string; render: (row: T) => React.ReactNode }[];
  rows: T[];
  rowKey: (row: T, i: number) => string;
  caption?: string;
}) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full border-collapse text-left text-sm">
        {caption && <caption className="sr-only">{caption}</caption>}
        <thead>
          <tr className="border-b border-slate-200 text-slate-700">
            {columns.map((c) => (
              <th key={c.header} scope="col" className="px-2 py-2 font-semibold">{c.header}</th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={rowKey(r, i)} className="border-b border-slate-100 last:border-0 hover:bg-slate-50">
              {columns.map((c) => (
                <td key={c.header} className="px-2 py-2 align-top text-slate-900">{c.render(r)}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
