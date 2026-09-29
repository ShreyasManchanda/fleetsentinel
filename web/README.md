# FleetSentinel web console (Stream E)

Vite + React + TypeScript + Tailwind + TanStack Query. Mock-first: every screen
works with no backend (typed mock implementing `contracts/openapi.yaml` shapes).

## Run

- Install: `npm.cmd install` (Windows PowerShell blocks `npm.ps1`; `npm.cmd` works)
- Dev (mock API): `npm.cmd run dev` → http://localhost:5173
- Dev (real API): set `VITE_API_BASE_URL=http://localhost:8000` in `.env`, restart
- Build: `npm.cmd run build` (runs `tsc --noEmit` + `vite build`)
- Typecheck: `npm.cmd run typecheck`
- Tests: `npm.cmd test` (Vitest)
- Lint: `npm.cmd run lint`

## Auth

OIDC Authorization Code + PKCE via `VITE_OIDC_ISSUER`. If unset, a clearly
labelled mock-token dev login is offered (never for the demo recording).
