"""HTTP gap tests: RBAC matrix, BOLA isolation, purpose gating, JWT edges,
rate limiting, idempotency lifecycle, cursor binding, report validation,
audit emission, DSR redaction-on-replay, resilience (503), security headers.

All I/O is faked via api_support/api_fakes (no live PG/Redis/Mongo/Vault).
"""
from __future__ import annotations

from api_support import TENANT_A, TENANT_B, VIN_A, build, sign

VIN_B = "1HGCM82633A004353"
VIN_C = "1HGCM82633A004354"

ACTION_BODY = {"proposal": {"kind": "inspect", "vin": VIN_A}, "rationale": "test"}


def _idem(h, key, **auth):
    return h.idem_headers(key, **auth)


# ---------------------------------------------------------------- RBAC matrix
def test_rbac_matrix_over_http(h):
    assert h.client.get("/v1/vehicles", headers=h.auth(roles=("viewer",))).status_code == 200
    assert h.client.get("/v1/mappings", headers=h.auth(roles=("viewer",))).status_code == 403
    r = h.client.post("/v1/agent/actions", json=ACTION_BODY,
                      headers=_idem(h, "viewer-act-01", roles=("viewer",)))
    assert r.status_code == 403, r.text
    assert h.client.get("/v1/audit", headers=h.auth(roles=("viewer",))).status_code == 403
    assert h.client.post("/v1/agent/chat", json={"message": "hi"},
                         headers=h.auth(roles=("viewer",))).status_code == 403
    # analyst can propose but not approve
    r = h.client.post("/v1/agent/actions", json=ACTION_BODY,
                      headers=_idem(h, "analyst-act-01", roles=("analyst",)))
    assert r.status_code == 201, r.text
    r = h.client.post(f"/v1/agent/actions/{r.json()['id']}/approve",
                      headers=_idem(h, "analyst-appr-01", roles=("analyst",)))
    assert r.status_code == 403
    # lender_agent: fleet read yes, agent queue no
    assert h.client.get("/v1/vehicles", headers=h.auth(roles=("lender_agent",))).status_code == 200
    assert h.client.get("/v1/agent/actions", headers=h.auth(roles=("lender_agent",))).status_code == 403
    # fleet_admin can read the audit log; platform_admin can list mappings
    assert h.client.get("/v1/audit", headers=h.auth()).status_code == 200
    assert h.client.get("/v1/mappings", headers=h.auth(roles=("platform_admin",))).status_code == 200
    assert h.client.get("/v1/vehicles").status_code == 401  # no bearer at all


def test_approve_reject_lifecycle_and_key_reuse(h):
    r = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "lifecycle-01"))
    assert r.status_code == 201, r.text
    aid = r.json()["id"]
    assert r.json()["status"] == "pending"
    # analyst (no AGENT_APPROVE) is denied
    r = h.client.post(f"/v1/agent/actions/{aid}/approve", headers=_idem(h, "lifecycle-02", roles=("analyst",)))
    assert r.status_code == 403
    # fleet_admin approves
    r = h.client.post(f"/v1/agent/actions/{aid}/approve", headers=_idem(h, "lifecycle-03"))
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "approved" and r.json()["work_order_id"]
    # same key + same path params replays the original approval, not a second work order
    r2 = h.client.post(f"/v1/agent/actions/{aid}/approve", headers=_idem(h, "lifecycle-03"))
    assert r2.status_code == 200 and r2.headers.get("Idempotent-Replay") == "true"
    assert r2.json()["work_order_id"] == r.json()["work_order_id"]
    # same key on a DIFFERENT action id is a payload mismatch, not a replay
    r = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "lifecycle-04"))
    aid2 = r.json()["id"]
    r = h.client.post(f"/v1/agent/actions/{aid2}/approve", headers=_idem(h, "lifecycle-03"))
    assert r.status_code == 422, r.text
    # reject path: rejected twice stays rejected; approving a rejected action is 409
    r = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "lifecycle-05"))
    rid = r.json()["id"]
    assert h.client.post(f"/v1/agent/actions/{rid}/reject", json={"reason": "no"},
                        headers=_idem(h, "lifecycle-06")).status_code == 200
    r = h.client.post(f"/v1/agent/actions/{rid}/reject", json={"reason": "no"},
                      headers=_idem(h, "lifecycle-07"))
    assert r.status_code == 200 and r.json()["status"] == "rejected"
    r = h.client.post(f"/v1/agent/actions/{rid}/approve", headers=_idem(h, "lifecycle-08"))
    assert r.status_code == 409


# ---------------------------------------------------------------- BOLA / cursors
def test_bola_tenant_isolation(h):
    h.data.add_vehicle(TENANT_A, VIN_A)
    assert h.client.get(f"/v1/vehicles/{VIN_A}", headers=h.auth(tenant=TENANT_B)).status_code == 404
    r = h.client.get("/v1/vehicles", headers=h.auth(tenant=TENANT_B))
    assert r.status_code == 200 and all(v["vin"] != VIN_A for v in r.json()["items"])


def test_pagination_cursor_bound_to_tenant(h):
    h.data.add_vehicle(TENANT_A, VIN_A)
    h.data.add_vehicle(TENANT_A, VIN_B)
    h.data.add_vehicle(TENANT_A, VIN_C)
    r = h.client.get("/v1/vehicles", params={"limit": 2}, headers=h.auth())
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["items"]) == 2 and body["next_cursor"]
    # another tenant cannot redeem this cursor
    r = h.client.get("/v1/vehicles", params={"cursor": body["next_cursor"]}, headers=h.auth(tenant=TENANT_B))
    assert r.status_code == 400
    # garbage is a 400, not a 500
    r = h.client.get("/v1/vehicles", params={"cursor": "junk!!"}, headers=h.auth())
    assert r.status_code == 400
    # owner follows the cursor to the last page (no further cursor)
    r = h.client.get("/v1/vehicles", params={"cursor": body["next_cursor"]}, headers=h.auth())
    assert r.status_code == 200 and len(r.json()["items"]) == 1
    assert r.json()["next_cursor"] is None


# ---------------------------------------------------------------- purpose + JWT
def test_purpose_gating(h):
    lender = h.auth(roles=("lender_agent",))
    r = h.client.get("/v1/vehicles", headers={**lender, "X-Purpose": "RECOVERY"})
    assert r.status_code == 200, r.text
    r = h.client.get("/v1/vehicles", headers={**h.auth(), "X-Purpose": "RECOVERY"})
    assert r.status_code == 403  # RECOVERY is lender_agent-only
    r = h.client.get("/v1/vehicles", headers={**lender, "X-Purpose": "BOGUS"})
    assert r.status_code == 422


def test_jwt_edges(h):
    import time

    import jwt as pyjwt

    assert h.client.get("/v1/me", headers=h.auth()).status_code == 200
    assert h.client.get("/v1/me").status_code == 401
    assert h.client.get("/v1/me", headers={"Authorization": "Token xyz"}).status_code == 401
    now = int(time.time())
    confusion = pyjwt.encode({"sub": "user-1", "iss": "http://localhost:8080/realms/fleetsentinel",
                              "aud": "fs-api", "exp": now + 300, "iat": now - 20, "nbf": now - 10,
                              "tenant_id": TENANT_A, "roles": ["fleet_admin"]},
                             "attacker-hmac-secret-0123456789abcdef", algorithm="HS256", headers={"kid": "test-key-1"})
    assert h.client.get("/v1/me", headers={"Authorization": "Bearer " + confusion}).status_code == 401
    assert h.client.get("/v1/me", headers=h.auth(exp_in=-100)).status_code == 401  # expired
    assert h.client.get("/v1/me", headers=h.auth(nbf_in=1000)).status_code == 401  # not yet valid
    assert h.client.get("/v1/me", headers=h.auth(aud="other")).status_code == 401
    assert h.client.get("/v1/me", headers=h.auth(kid="unknown-kid")).status_code == 401
    forged = sign(h.other_private)  # right kid, wrong key
    assert h.client.get("/v1/me", headers={"Authorization": "Bearer " + forged}).status_code == 401
    assert h.client.get("/v1/me", headers=h.auth(drop=("tenant_id",))).status_code == 403
    assert h.client.get("/v1/me", headers=h.auth(roles=())).status_code == 403


# ---------------------------------------------------------------- rate limiting
def test_rate_limit_429_over_http(rsa_private, rsa_other):
    h2 = build(rsa_private, rsa_other, rate_user_per_min=2, rate_tenant_per_min=1000)
    assert h2.client.get("/v1/me", headers=h2.auth()).status_code == 200
    assert h2.client.get("/v1/me", headers=h2.auth()).status_code == 200
    r = h2.client.get("/v1/me", headers=h2.auth())
    assert r.status_code == 429
    assert "Retry-After" in r.headers and "RateLimit-Limit" in r.headers
    assert r.headers["content-type"].startswith("application/problem+json")


# ---------------------------------------------------------------- idempotency
def test_idempotency_lifecycle(h):
    r1 = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "idem-key-01"))
    assert r1.status_code == 201, r1.text
    r2 = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "idem-key-01"))
    assert r2.status_code == 201 and r2.json()["id"] == r1.json()["id"]
    assert r2.headers.get("Idempotent-Replay") == "true"
    other = {"proposal": {"kind": "other"}, "rationale": "r"}
    r3 = h.client.post("/v1/agent/actions", json=other, headers=_idem(h, "idem-key-01"))
    assert r3.status_code == 422  # same key, different payload
    r4 = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=h.auth())
    assert r4.status_code == 400  # key required
    scope = f"idem:{TENANT_A}:user-1:POST:/v1/agent/actions:idem-key-02"
    r0 = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "idem-key-02"))
    assert r0.status_code == 201
    h.idem.d[scope] = {"state": "in_flight", "fp": h.idem.d[scope]["fp"]}  # stuck in flight
    r5 = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "idem-key-02"))
    assert r5.status_code == 409


# ---------------------------------------------------------------- DSR redaction
def test_dsr_access_export_redacted_on_replay(h):
    drv = h.data.add_driver(TENANT_A, "key-access-1", None, None, None)
    payload = {"driver_id": drv["id"], "kind": "ACCESS"}
    r1 = h.client.post("/v1/dsr", json=payload, headers=_idem(h, "dsr-key-01"))
    assert r1.status_code == 202, r1.text
    assert "export" in r1.json()["evidence"]
    r2 = h.client.post("/v1/dsr", json=payload, headers=_idem(h, "dsr-key-01"))
    assert r2.status_code == 202
    assert r2.json()["evidence"]["export"] == "[redacted: available only in the original response]"


def test_dsr_erase_already_erased_completes(h):
    drv = h.data.add_driver(TENANT_A, "key-erase-1", None, None, None)
    r = h.client.post("/v1/dsr", json={"driver_id": drv["id"], "kind": "ERASE"}, headers=_idem(h, "dsr-key-02"))
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "completed"
    assert body["evidence"]["already_erased"] is True
    assert body["evidence"]["replay_check"]["unreadable"] is True


# ---------------------------------------------------------------- reports
def test_report_validation_over_http(h):
    r = h.client.post("/v1/reports/batch", json={"report_id": "nope", "params": {}}, headers=h.auth())
    assert r.status_code == 422  # not a predefined report
    r = h.client.post("/v1/reports/batch",
                      json={"report_id": "carbon_by_fleet", "params": {"from": "2026-01-01", "to": "yesterday"}},
                      headers=h.auth())
    assert r.status_code == 422
    r = h.client.post("/v1/reports/batch",
                      json={"report_id": "carbon_by_fleet", "params": {"from": "2026-01-01", "to": "2026-01-31"}},
                      headers=h.auth())
    assert r.status_code == 200, r.text
    r = h.client.get("/v1/reports/cost", params={"from": "2026-02-01", "to": "2026-01-01"}, headers=h.auth())
    assert r.status_code == 422  # inverted range
    r = h.client.get("/v1/reports/cost", params={"from": "2026-01-01", "to": "2026-01-31"}, headers=h.auth())
    assert r.status_code == 200, r.text
    r = h.client.get("/v1/insights/export",
                     params={"from": "2024-01-01", "to": "2026-06-01"}, headers=h.auth())
    assert r.status_code == 422  # range exceeds 366 days
    r = h.client.get("/v1/insights/export",
                     params={"from": "2026-01-01", "to": "2026-01-10"}, headers=h.auth())
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- audit + ops
def test_audit_emitted_allow_and_deny(h):
    n0 = len(h.audit.records)
    assert h.client.get("/v1/vehicles", headers=h.auth()).status_code == 200
    assert len(h.audit.records) == n0 + 1 and h.audit.records[-1].decision == "ALLOW"
    r = h.client.post("/v1/agent/actions", json=ACTION_BODY, headers=_idem(h, "audit-01", roles=("viewer",)))
    assert r.status_code == 403
    assert h.audit.records[-1].decision == "DENY"


def test_audit_verify_endpoint(h):
    r = h.client.get("/v1/audit/verify", headers=h.auth())
    assert r.status_code == 200 and r.json()["ok"] is True


def test_security_headers_and_trace_id(h):
    r = h.client.get("/v1/me", headers=h.auth())
    assert r.status_code == 200
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers.get("x-trace-id")


def test_dependency_outage_maps_to_503(h):
    h.db.fail_next = ConnectionError("pg down")
    r = h.client.get("/v1/vehicles", headers=h.auth())
    assert r.status_code == 503
    assert r.headers["content-type"].startswith("application/problem+json")
    assert h.client.get("/v1/vehicles", headers=h.auth()).status_code == 200  # fail_next consumed
