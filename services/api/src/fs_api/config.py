"""Runtime configuration (env prefix FS_API_). Secrets have DEV-ONLY defaults that match deploy/compose."""
from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FS_API_", extra="ignore")

    env: str = "dev"  # dev | prod. prod refuses DEV-ONLY secrets and LocalDevWrapper (see main.py)

    # data stores (adapters)
    database_url: str = "postgresql+psycopg://fs_app:fs_app_dev@localhost:5432/fleetsentinel"  # fs_app: RLS applies
    worker_database_url: str | None = None  # fs_worker: only used for the DSR trip pseudonymization fallback
    redis_url: str = "redis://localhost:6379/0"
    mongo_url: str = "mongodb://localhost:27017"
    mongo_db: str = "fleetsentinel"
    kafka_bootstrap: str = "localhost:9092"
    agent_url: str = "http://localhost:8100"
    lake_uri: str = "s3://fs-lake/telemetry/**/*.parquet"

    # identity
    oidc_issuer: str = "http://localhost:8080/realms/fleetsentinel"
    oidc_jwks_url: str | None = None  # default: <issuer>/protocol/openid-connect/certs
    oidc_audience: str = "fs-api"
    jwt_leeway_s: int = 30
    jwks_ttl_s: int = 600
    jwks_min_refresh_s: int = 30  # rate limit for forced refresh on an unknown kid (rotation) so junk kids cannot hammer Keycloak

    # crypto
    vault_addr: str = "http://localhost:8200"
    vault_token: str = "root"  # noqa: S105  DEV-ONLY (compose dev Vault)
    vault_key: str = "fs-kek"
    allow_local_dev_wrapper: bool = False  # DEV ONLY: LocalDevWrapper when Vault is not reachable
    dek_cache_size: int = 1024

    # API behaviour
    cursor_secret: str = "dev-only-cursor-secret-change-me"  # noqa: S105  HMAC key for opaque cursors
    cors_origins: list[str] = Field(default_factory=lambda: ["http://localhost:5173", "http://localhost:3000"])
    body_limit_bytes: int = 1_048_576
    audit_skip_paths: list[str] = Field(default_factory=lambda: ["/healthz", "/readyz", "/metrics"])
    four_eyes_mapping_approval: bool = False  # approver must differ from the creator

    # rate limits (per minute)
    rate_tenant_per_min: int = 1000
    rate_user_per_min: int = 300
    rate_copilot_per_min: int = 20

    # circuit breakers
    breaker_failures: int = 5
    breaker_reset_s: float = 10.0

    # decision economics (spec 10.1: EV = p * C_breakdown - C_inspection; tenant-level config is a future table)
    cost_breakdown_usd: float = 1800.0
    cost_inspection_usd: float = 120.0
    min_exposure_km: float = 200.0

    idempotency_ttl_s: int = 86_400
    ws_tick_s: float = 1.0
    ws_auth_timeout_s: float = 5.0
    ws_max_bad_messages: int = 20
    export_k: int = 10

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer.rstrip('/')}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
