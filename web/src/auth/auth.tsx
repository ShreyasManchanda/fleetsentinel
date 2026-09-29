import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import { setToken as persistToken, getToken, USE_MOCK } from '../api/client';

// OIDC Authorization Code + PKCE against Keycloak (config-driven).
// When no Keycloak is reachable (normal in dev), fall back to a mock-token
// dev mode that is CLEARLY labelled in the UI and never used in the demo.

const OIDC_ISSUER = import.meta.env.VITE_OIDC_ISSUER as string | undefined;
const OIDC_CLIENT_ID = (import.meta.env.VITE_OIDC_CLIENT_ID as string | undefined) ?? 'fleetsentinel-web';
const OIDC_REDIRECT = (import.meta.env.VITE_OIDC_REDIRECT as string | undefined) ?? window.location.origin + '/';

interface AuthState {
  token: string | null;
  mockMode: boolean;
  oidcConfigured: boolean;
  loginOidc: () => void;
  loginMock: () => void;
  logout: () => void;
}

const AuthCtx = createContext<AuthState | null>(null);

function base64url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setTok] = useState<string | null>(getToken());
  const oidcConfigured = Boolean(OIDC_ISSUER);

  // If we land back from Keycloak with ?code=, finish the code exchange.
  const finishCodeExchange = useCallback(async () => {
    const url = new URL(window.location.href);
    const code = url.searchParams.get('code');
    const verifier = sessionStorage.getItem('pkce_verifier');
    if (!code || !verifier || !OIDC_ISSUER) return;
    const res = await fetch(`${OIDC_ISSUER}/protocol/openid-connect/token`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({
        grant_type: 'authorization_code',
        client_id: OIDC_CLIENT_ID,
        code,
        redirect_uri: OIDC_REDIRECT,
        code_verifier: verifier,
      }),
    });
    if (!res.ok) throw new Error(`OIDC token exchange failed: ${res.status}`);
    const data = (await res.json()) as { access_token: string };
    persistToken(data.access_token);
    setTok(data.access_token);
    sessionStorage.removeItem('pkce_verifier');
    url.searchParams.delete('code');
    url.searchParams.delete('state');
    window.history.replaceState({}, '', url.toString());
  }, []);

  const loginOidc = useCallback(() => {
    if (!OIDC_ISSUER) return;
    const verifierBytes = crypto.getRandomValues(new Uint8Array(32));
    const verifier = base64url(verifierBytes);
    sessionStorage.setItem('pkce_verifier', verifier);
    crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier)).then((hash) => {
      const challenge = base64url(new Uint8Array(hash));
      const auth = new URL(`${OIDC_ISSUER}/protocol/openid-connect/auth`);
      auth.searchParams.set('client_id', OIDC_CLIENT_ID);
      auth.searchParams.set('redirect_uri', OIDC_REDIRECT);
      auth.searchParams.set('response_type', 'code');
      auth.searchParams.set('scope', 'openid profile email');
      auth.searchParams.set('code_challenge', challenge);
      auth.searchParams.set('code_challenge_method', 'S256');
      window.location.assign(auth.toString());
    }).catch((e) => console.error('PKCE setup failed', e));
  }, []);

  const loginMock = useCallback(() => {
    persistToken('mock-dev-token');
    setTok('mock-dev-token');
  }, []);

  const logout = useCallback(() => {
    persistToken(null);
    setTok(null);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      token,
      mockMode: USE_MOCK || token === 'mock-dev-token',
      oidcConfigured,
      loginOidc,
      loginMock,
      logout,
    }),
    [token, oidcConfigured, loginOidc, loginMock, logout],
  );

  // Fire-and-forget code exchange on first mount.
  useState(() => { void finishCodeExchange(); });

  return <AuthCtx.Provider value={value}>{children}</AuthCtx.Provider>;
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthCtx);
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider');
  return ctx;
}
