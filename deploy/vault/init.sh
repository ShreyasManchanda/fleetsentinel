#!/bin/sh
# Idempotent Vault bootstrap (dev). Transit holds the KEK that wraps per-subject data keys (crypto-shredding, ADR-04).
set -eu
vault secrets enable transit 2>/dev/null || true
vault write -f transit/keys/fs-kek type=aes256-gcm96 2>/dev/null || true
vault policy write fs-pii - <<'POLICY'
path "transit/encrypt/fs-kek"  { capabilities = ["update"] }
path "transit/decrypt/fs-kek"  { capabilities = ["update"] }
path "transit/rewrap/fs-kek"   { capabilities = ["update"] }
POLICY
vault kv put secret/fleetsentinel/dev note="dev-only secrets live in compose env; production reads from Vault via the K8s auth method" >/dev/null 2>&1 || true
echo "vault init ok: transit key fs-kek ready"
