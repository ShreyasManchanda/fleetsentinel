import { Suspense, lazy } from 'react';
import { NavLink, Route, Routes, Navigate } from 'react-router-dom';
import { API_BASE, USE_MOCK } from './api/client';
import { useAuth } from './auth/auth';
import { Button, Loading } from './components/ui';
// Code-split: deck.gl + maplibre (~1.5 MB) load only on the Command Center route.
const CommandCenter = lazy(() => import('./pages/CommandCenter'));
import Vehicle360 from './pages/Vehicle360';
import Copilot from './pages/Copilot';
import Drivers from './pages/Drivers';
import Platform from './pages/Platform';
import Compliance from './pages/Compliance';

const LINKS = [
  { to: '/', label: 'Command Center' },
  { to: '/vehicles', label: 'Vehicle 360' },
  { to: '/copilot', label: 'Copilot' },
  { to: '/drivers', label: 'Drivers' },
  { to: '/platform', label: 'Platform' },
  { to: '/compliance', label: 'Compliance' },
];

function LoginGate() {
  const { loginOidc, loginMock, oidcConfigured } = useAuth();
  return (
    <main className="mx-auto max-w-md space-y-4 p-8 text-center">
      <h1 className="text-2xl font-bold text-slate-900">FleetSentinel Console</h1>
      <p className="text-sm text-slate-700">Sign in to continue.</p>
      {oidcConfigured && <Button onClick={loginOidc}>Sign in with Keycloak (OIDC + PKCE)</Button>}
      <div>
        <Button variant="ghost" onClick={loginMock}>Continue with mock token (DEV ONLY — never in demo recording)</Button>
        {!oidcConfigured && <p className="mt-1 text-xs text-slate-600">No Keycloak reachable: VITE_OIDC_ISSUER unset.</p>}
      </div>
    </main>
  );
}

export default function App() {
  const { token, mockMode, logout } = useAuth();
  if (!token) return <LoginGate />;
  return (
    <div className="min-h-screen bg-slate-50 text-slate-900">
      <a href="#main" className="sr-only focus:not-sr-only focus:absolute focus:bg-white focus:p-2">Skip to content</a>
      {mockMode && (
        <div role="note" className="bg-amber-300 px-4 py-1 text-center text-xs font-bold text-amber-950">
          DEV MOCK MODE — mock token + mock API ({API_BASE}). NEVER use in the demo recording. Set VITE_API_BASE_URL + VITE_OIDC_ISSUER for real.
        </div>
      )}
      {!USE_MOCK && (
        <div role="note" className="bg-emerald-200 px-4 py-1 text-center text-xs font-bold text-emerald-950">
          LIVE API — {API_BASE}
        </div>
      )}
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-2 px-4 py-3">
          <span className="mr-4 text-lg font-bold">FleetSentinel</span>
          <nav aria-label="Primary" className="flex flex-wrap gap-1">
            {LINKS.map((l) => (
              <NavLink
                key={l.to}
                to={l.to}
                end={l.to === '/'}
                className={({ isActive }) =>
                  `rounded-md px-3 py-1.5 text-sm font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-sky-600 ${
                    isActive ? 'bg-slate-900 text-white' : 'text-slate-800 hover:bg-slate-100'
                  }`
                }
              >
                {l.label}
              </NavLink>
            ))}
          </nav>
          <span className="ml-auto" />
          <Button variant="ghost" onClick={logout}>Sign out</Button>
        </div>
      </header>
      <main id="main" className="mx-auto max-w-6xl px-4 py-4">
        <Suspense fallback={<Loading label="Loading screen…" />}>
        <Routes>
          <Route path="/" element={<CommandCenter />} />
          <Route path="/vehicles" element={<Vehicle360 />} />
          <Route path="/vehicles/:vin" element={<Vehicle360 />} />
          <Route path="/copilot" element={<Copilot />} />
          <Route path="/drivers" element={<Drivers />} />
          <Route path="/platform" element={<Platform />} />
          <Route path="/compliance" element={<Compliance />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
        </Suspense>
      </main>
    </div>
  );
}
