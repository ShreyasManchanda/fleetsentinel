"""HTTP adapters: Keycloak JWKS fetch and the copilot (agent) service client."""
from __future__ import annotations

from typing import Any

import httpx
import jwt

from ..ports import AgentUnavailable


def fetch_jwks_keys(url: str, timeout: float = 3.0) -> dict[str, Any]:
    """Blocking fetch returning {kid: RSA public key}. Only RS256 signing keys are accepted."""
    r = httpx.get(url, timeout=timeout)
    r.raise_for_status()
    keys: dict[str, Any] = {}
    for jwk in r.json().get("keys", []):
        if jwk.get("kty") == "RSA" and jwk.get("use", "sig") == "sig" and jwk.get("alg", "RS256") == "RS256" and jwk.get("kid"):
            keys[jwk["kid"]] = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
    return keys


class HttpAgentClient:
    """Proxy to services/agent. The caller bearer token is forwarded so the agent calls back into this API as the user
    (RLS and RBAC keep applying); the tenant is never taken from the body."""

    def __init__(self, base_url: str, timeout: float = 20.0):
        self._c = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def _post(self, path: str, bearer: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self._c.post(path, json=body, headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError as e:
            raise AgentUnavailable(str(e)) from e
        if r.status_code >= 500 or r.status_code == 503:
            raise AgentUnavailable(f"agent returned {r.status_code}")
        r.raise_for_status()
        return r.json()

    async def chat(self, bearer, tenant_id, message, conversation_id):
        return await self._post("/chat", bearer, {"message": message, "conversation_id": conversation_id})

    async def briefing(self, bearer, tenant_id, generate):
        if generate:
            return await self._post("/briefing", bearer, {})
        try:
            r = await self._c.get("/briefing", headers={"Authorization": f"Bearer {bearer}"})
        except httpx.HTTPError as e:
            raise AgentUnavailable(str(e)) from e
        if r.status_code >= 500:
            raise AgentUnavailable(f"agent returned {r.status_code}")
        r.raise_for_status()
        return r.json()
