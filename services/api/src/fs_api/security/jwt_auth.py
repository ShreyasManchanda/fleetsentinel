"""Keycloak JWT validation. RS256 is PINNED (alg=none / HS256 confusion attacks rejected), and iss, aud, exp, nbf are
enforced. The tenant comes ONLY from the tenant_id claim. The JWKS provider is injectable so tests use a local RSA key."""
from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import jwt
from jwt.exceptions import InvalidTokenError

from ..domain.roles import Role, parse_roles
from ..errors import ProblemError, forbidden, unauthorized

ALLOWED_ALGS = ["RS256"]


@dataclass(frozen=True, slots=True)
class Principal:
    sub: str
    tenant_id: str
    roles: frozenset[Role]
    email: str | None = None
    token: str = ""
    exp: int = 0

    @property
    def actor_id(self) -> str:
        return self.sub


class JwksProvider(Protocol):
    def get_key(self, kid: str) -> Any:
        """Return the public key for `kid` (refreshing on an unknown kid to support rotation) or raise KeyError."""
        ...


class StaticJwksProvider:
    """Fixed key set: unit tests and offline dev. `rotate` swaps keys the way a JWKS refresh would."""

    def __init__(self, keys: dict[str, Any]):
        self._keys = dict(keys)

    def get_key(self, kid: str) -> Any:
        return self._keys[kid]

    def rotate(self, keys: dict[str, Any]) -> None:
        self._keys = dict(keys)


class CachingJwksProvider:
    """JWKS cache with rotation: entries live `ttl_s`; an UNKNOWN kid triggers one refresh (rate limited by
    `min_refresh_s`, so a flood of junk kids cannot hammer the identity provider). `fetch` returns {kid: public_key}."""

    def __init__(self, fetch: Callable[[], dict[str, Any]], ttl_s: float = 600, min_refresh_s: float = 30,
                 clock: Callable[[], float] = time.monotonic):
        self._fetch, self._ttl, self._min, self._clock = fetch, ttl_s, min_refresh_s, clock
        self._keys: dict[str, Any] = {}
        self._fetched_at = -1e18
        self.fetches = 0

    def _refresh(self) -> None:
        self._keys = self._fetch()
        self._fetched_at = self._clock()
        self.fetches += 1

    def get_key(self, kid: str) -> Any:
        now = self._clock()
        if now - self._fetched_at > self._ttl:
            self._refresh()
        if kid in self._keys:
            return self._keys[kid]
        if now - self._fetched_at >= self._min:  # unknown kid: maybe rotated
            self._refresh()
            if kid in self._keys:
                return self._keys[kid]
        raise KeyError(kid)


class JwtValidator:
    def __init__(self, jwks: JwksProvider, issuer: str, audience: str, leeway_s: int = 30):
        self._jwks, self._iss, self._aud, self._leeway = jwks, issuer, audience, leeway_s

    def validate(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except InvalidTokenError as e:
            raise unauthorized("malformed token") from e
        if header.get("alg") not in ALLOWED_ALGS:
            raise unauthorized("unsupported signing algorithm")
        kid = header.get("kid")
        if not kid:
            raise unauthorized("token has no kid")
        try:
            key = self._jwks.get_key(kid)
        except KeyError as e:
            raise unauthorized("unknown signing key") from e
        except Exception as e:  # JWKS endpoint unreachable and no cached key: identity provider outage, not a bad token
            raise ProblemError(503, "Service Unavailable", "identity provider keys unavailable", "idp-unavailable",
                               {"Retry-After": "5"}) from e
        try:
            claims = jwt.decode(
                token, key, algorithms=ALLOWED_ALGS, audience=self._aud, issuer=self._iss, leeway=self._leeway,
                options={"require": ["exp", "iss", "aud", "sub"], "verify_signature": True, "verify_exp": True,
                         "verify_nbf": True, "verify_aud": True, "verify_iss": True},
            )
        except InvalidTokenError as e:
            raise unauthorized(f"invalid token: {type(e).__name__}") from e
        return self._principal(claims, token)

    @staticmethod
    def _principal(claims: dict[str, Any], token: str) -> Principal:
        tenant = claims.get("tenant_id")
        try:
            tenant = str(uuid.UUID(str(tenant)))
        except (ValueError, TypeError, AttributeError):
            raise forbidden("token carries no valid tenant_id claim", "no-tenant") from None
        raw_roles = claims.get("roles")
        if raw_roles is None:
            raw_roles = (claims.get("realm_access") or {}).get("roles")
        roles = parse_roles(raw_roles)
        if not roles:
            raise forbidden("token carries no recognised role", "no-role")
        return Principal(sub=str(claims["sub"]), tenant_id=tenant, roles=roles, email=claims.get("email"),
                         token=token, exp=int(claims.get("exp", 0)))
