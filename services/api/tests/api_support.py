from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fs_api.api.app import create_app
from fs_api.config import Settings
from fs_api.container import Deps
from fs_api.domain.audit import ListAuditPublisher, ResilientAuditPublisher
from fs_api.domain.ratelimit import InMemoryBucketStore, RateLimiter
from fs_api.metrics import Metrics
from fs_api.security.jwt_auth import JwtValidator, StaticJwksProvider
from fscore.crypto import LocalDevWrapper, SubjectKeyStore
from starlette.testclient import TestClient

import api_fakes as fakes

ISS = "http://localhost:8080/realms/fleetsentinel"
AUD = "fs-api"
KID = "test-key-1"
TENANT_A = "a1968e1a-1866-53ab-ada0-79dde61e800d"
TENANT_B = "b2b2b2b2-1866-53ab-ada0-79dde61e800d"
VIN_A = "1HGCM82633A004352"
VIN_B = "JH4KA8270MC000000"


def sign(private_key, *, sub="user-1", tenant=TENANT_A, roles=("fleet_admin",), exp_in=300, nbf_in=-10, aud=AUD, iss=ISS,
         kid=KID, alg="RS256", extra: dict[str, Any] | None = None, drop: tuple[str, ...] = ()) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"sub": sub, "iss": iss, "aud": aud, "exp": now + exp_in, "iat": now - 20, "nbf": now + nbf_in,
                              "tenant_id": tenant, "roles": list(roles), "email": f"{sub}@demo.test"}
    claims.update(extra or {})
    for k in drop:
        claims.pop(k, None)
    if alg == "RS256":
        key = private_key
    else:  # HS256 confusion attack: sign with the PUBLIC key bytes as the HMAC secret
        key = private_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


@dataclass
class Harness:
    app: Any
    client: TestClient
    deps: Deps
    data: fakes.Data
    db: fakes.FakeTenantDb
    state: fakes.FakeState
    twins: fakes.FakeTwins
    feed: fakes.FakeAlertFeed
    pub: fakes.FakeMappingPublisher
    agent: fakes.FakeAgent
    quality: fakes.FakeQuality
    idem: fakes.MemoryIdem
    audit: ListAuditPublisher
    wrapper: LocalDevWrapper
    keys: SubjectKeyStore
    settings: Settings
    private: Any
    other_private: Any
    jwks: StaticJwksProvider
    tokens: dict = field(default_factory=dict)

    def token(self, **kw) -> str:
        return sign(self.private, **kw)

    def auth(self, roles=("fleet_admin",), tenant=TENANT_A, sub="user-1", **kw) -> dict[str, str]:
        return {"Authorization": "Bearer " + self.token(sub=sub, tenant=tenant, roles=roles, **kw)}

    def idem_headers(self, key: str | None = None, **auth) -> dict[str, str]:
        return {**self.auth(**auth), "Idempotency-Key": key or uuid.uuid4().hex}


def build(rsa_private, rsa_other, **settings_kw) -> Harness:
    kw = {"env": "dev", "cursor_secret": "test-cursor-secret", "ws_tick_s": 0.05, "ws_auth_timeout_s": 1.0, **settings_kw}
    s = Settings(**kw)
    data = fakes.Data()
    db = fakes.FakeTenantDb(data)
    state, twins, feed = fakes.FakeState(), fakes.FakeTwins(), fakes.FakeAlertFeed()
    pub, agent, quality, idem = fakes.FakeMappingPublisher(), fakes.FakeAgent(), fakes.FakeQuality(), fakes.MemoryIdem()
    audit = ListAuditPublisher()
    wrapper = LocalDevWrapper()
    keys = SubjectKeyStore(wrapper)
    jwks = StaticJwksProvider({KID: rsa_private.public_key()})
    deps = Deps(
        settings=s, validator=JwtValidator(jwks, ISS, AUD, s.jwt_leeway_s), db=db, state=state, twins=twins, alert_feed=feed,
        mapping_pub=pub, agent=agent, reports=fakes.FakeReportRunner(), quality=quality, idem=idem,
        limiter=RateLimiter(InMemoryBucketStore()), audit=ResilientAuditPublisher(audit), keys=keys, metrics=Metrics(),
        probes={"postgres": db.ping, "redis": state.ping},
    )
    app = create_app(deps)
    client = TestClient(app, raise_server_exceptions=False)
    return Harness(app, client, deps, data, db, state, twins, feed, pub, agent, quality, idem, audit, wrapper, keys, s, rsa_private,
                   rsa_other, jwks)
