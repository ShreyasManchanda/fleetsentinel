"""Generate the Keycloak realm import (deploy/keycloak/realm-fleetsentinel.json) from contracts/demo_tenants.yaml.

- Realm roles: platform_admin, fleet_admin, analyst, viewer, lender_agent  -> `roles` claim.
- User attribute tenant_id (deterministic uuid5) -> `tenant_id` claim. The API sets `app.tenant_id` from THIS claim only.
- Clients: fs-web (public, Authorization Code + PKCE S256; direct-access enabled for scripts/k6 in DEV only),
           fs-api (bearer-only resource server; audience `fs-api`).
DEV-ONLY: sslRequired none, shared demo password. Regenerate: python tools/gen_realm.py
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml
from fscore import contracts

ROOT = Path(__file__).resolve().parent.parent
ROLES = ["platform_admin", "fleet_admin", "analyst", "viewer", "lender_agent"]


def mapper(name: str, typ: str, cfg: dict) -> dict:
    return {"name": name, "protocol": "openid-connect", "protocolMapper": typ, "consentRequired": False, "config": cfg}


CLAIM_MAPPERS = [
    mapper("tenant_id", "oidc-usermodel-attribute-mapper", {
        "user.attribute": "tenant_id", "claim.name": "tenant_id", "jsonType.label": "String",
        "id.token.claim": "true", "access.token.claim": "true", "userinfo.token.claim": "true"}),
    mapper("roles", "oidc-usermodel-realm-role-mapper", {
        "claim.name": "roles", "multivalued": "true", "jsonType.label": "String",
        "id.token.claim": "true", "access.token.claim": "true", "userinfo.token.claim": "true"}),
    mapper("aud-fs-api", "oidc-audience-mapper", {
        "included.client.audience": "fs-api", "id.token.claim": "false", "access.token.claim": "true"}),
]


def user(username: str, tenant_slug: str, role: str, pw: str) -> dict:
    return {
        "username": username, "email": username, "emailVerified": True, "enabled": True,
        "firstName": username.split("@")[0].replace("-", " ").title(), "lastName": "Demo",
        "credentials": [{"type": "password", "value": pw, "temporary": False}],
        "realmRoles": [role],
        "attributes": {"tenant_id": [contracts.tenant_id(tenant_slug)]},
    }


def main() -> int:
    cfg = yaml.safe_load((ROOT / "contracts" / "demo_tenants.yaml").read_text(encoding="utf-8"))
    pw = cfg["password"]
    users = []
    for t in cfg["tenants"]:
        slug = t["slug"]
        for role in cfg["user_roles_per_tenant"]:
            prefix = {"fleet_admin": "admin", "analyst": "analyst", "viewer": "viewer"}[role]
            users.append(user(f"{prefix}@{slug}.demo", slug, role, pw))
        users.append(user(f"briefing-bot@{slug}.demo", slug, "analyst", pw))
        if t.get("lender"):
            users.append(user(f"lender@{slug}.demo", slug, "lender_agent", pw))
    pa = cfg["platform_admin"]
    users.append(user(pa["username"], pa["tenant"], "platform_admin", pw))

    realm = {
        "realm": "fleetsentinel", "enabled": True, "sslRequired": "none",
        "registrationAllowed": False, "loginWithEmailAllowed": True, "duplicateEmailsAllowed": False,
        "accessTokenLifespan": 900, "ssoSessionIdleTimeout": 28800, "bruteForceProtected": True,
        "roles": {"realm": [{"name": r, "description": f"FleetSentinel {r}"} for r in ROLES]},
        "clients": [
            {"clientId": "fs-web", "name": "FleetSentinel Web", "enabled": True, "publicClient": True,
             "standardFlowEnabled": True, "directAccessGrantsEnabled": True, "implicitFlowEnabled": False,
             "redirectUris": ["http://localhost:5173/*", "http://localhost:8088/*", "https://*/*"],
             "webOrigins": ["+"], "attributes": {"pkce.code.challenge.method": "S256"},
             "protocolMappers": CLAIM_MAPPERS},
            {"clientId": "fs-api", "name": "FleetSentinel API", "enabled": True, "bearerOnly": True},
        ],
        "users": users,
    }
    out = ROOT / "deploy" / "keycloak" / "realm-fleetsentinel.json"
    out.write_text(json.dumps(realm, indent=2), encoding="utf-8")
    print(f"wrote {out} ({len(users)} users, {len(cfg['tenants'])} named tenants)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
