# Free demo URL via Cloudflare Tunnel (no cloud account, no card)

Use this when the Terraform VM is not up: expose the laptop (or Codespace) stack as a
public HTTPS URL for judging and for the demo-video recording.

## On the machine running the stack

```bash
# 1. one-time install (Windows):
winget install Cloudflare.cloudflared
# 2. expose the web console (or the API on 8000):
cloudflared tunnel --url http://localhost:5173
# => https://<random>.trycloudflare.com  (paste this into the demo + Solution Document)
```

For a stable name across restarts (needs a free Cloudflare account, still no card):
`cloudflared tunnel create fleetsentinel-demo` once, then
`cloudflared tunnel route dns fleetsentinel-demo demo.example.com`.

## Notes
- The machine must stay awake with Docker running for the whole judging window.
- The API behind it is the same RBAC + RLS build the BOLA tests prove — the tunnel adds
  TLS, it does not bypass auth.
- Alternative with zero installs on the laptop: run the stack in the Codespace
  (.devcontainer is committed) and forward port 5173 (Visibility: Public) — GitHub gives
  you a `*.app.github.dev` URL for free.
