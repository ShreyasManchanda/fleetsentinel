"""Process entrypoint wiring (excluded from the unit-coverage gate; exercised by the integration tests).

Run:  uvicorn fs_api.main:create --factory --host 0.0.0.0 --port 8000        (Linux/container)
      python -m fs_api                                                         (any OS; uses a Selector event loop for psycopg async)
"""
from __future__ import annotations

import logging

import structlog
from fastapi import FastAPI
from fscore.crypto import LocalDevWrapper, SubjectKeyStore, VaultTransitWrapper

from .adapters.duckdb_reports import DuckDbReportRunner
from .adapters.http_clients import HttpAgentClient, fetch_jwks_keys
from .adapters.kafka_pub import KafkaAuditPublisher, KafkaMappingPublisher, make_producer
from .adapters.mongo_store import MongoDlq, MongoTwinStore, make_mongo
from .adapters.pg import PgTenantDb, make_engine
from .adapters.redis_store import (
    RedisAlertFeed,
    RedisBucketStore,
    RedisIdempotencyStore,
    RedisQualityStore,
    RedisStateStore,
    make_redis,
)
from .api.app import create_app
from .config import Settings, get_settings
from .container import Deps
from .domain.audit import ResilientAuditPublisher
from .domain.breaker import BreakerProxy
from .domain.ratelimit import RateLimiter
from .guarded import GuardedTenantDb, make_breaker
from .metrics import Metrics
from .security.jwt_auth import CachingJwksProvider, JwtValidator

DEV_CURSOR_SECRET = "dev-only-cursor-secret-change-me"  # noqa: S105


def check_production_safety(s: Settings) -> None:
    if s.env != "dev" and (s.cursor_secret == DEV_CURSOR_SECRET or s.allow_local_dev_wrapper or s.vault_token == "root"):  # noqa: S105
        raise RuntimeError("refusing to start with DEV-ONLY secrets (cursor secret, Vault root token, LocalDevWrapper) outside env=dev")


def build_deps(s: Settings) -> Deps:
    check_production_safety(s)
    metrics = Metrics()
    breakers = {n: make_breaker(n, s, metrics) for n in ("postgres", "redis", "mongo", "agent")}
    engine = make_engine(s.database_url)
    priv = make_engine(s.worker_database_url, 2) if s.worker_database_url else None
    db = GuardedTenantDb(PgTenantDb(engine, priv), breakers["postgres"])
    r = make_redis(s.redis_url)
    state = BreakerProxy(RedisStateStore(r), breakers["redis"])
    mongo = make_mongo(s.mongo_url)
    producer = make_producer(s.kafka_bootstrap)
    audit_kafka = KafkaAuditPublisher(producer)
    if s.allow_local_dev_wrapper:
        logging.getLogger("fs_api").warning("USING LocalDevWrapper: DEV ONLY, not protected by Vault")
        wrapper = LocalDevWrapper()
    else:
        wrapper = VaultTransitWrapper(s.vault_addr, s.vault_token, s.vault_key)
    jwks = CachingJwksProvider(lambda: fetch_jwks_keys(s.jwks_url), s.jwks_ttl_s, s.jwks_min_refresh_s)
    return Deps(
        settings=s, validator=JwtValidator(jwks, s.oidc_issuer, s.oidc_audience, s.jwt_leeway_s), db=db, state=state,
        twins=BreakerProxy(MongoTwinStore(mongo, s.mongo_db), breakers["mongo"]), alert_feed=RedisAlertFeed(r),
        mapping_pub=KafkaMappingPublisher(producer), agent=BreakerProxy(HttpAgentClient(s.agent_url), breakers["agent"]),
        reports=DuckDbReportRunner(s.lake_uri), quality=RedisQualityStore(r, MongoDlq(mongo, producer, s.mongo_db)),
        idem=BreakerProxy(RedisIdempotencyStore(r), breakers["redis"]), limiter=RateLimiter(BreakerProxy(RedisBucketStore(r), breakers["redis"])), audit=ResilientAuditPublisher(audit_kafka),
        keys=SubjectKeyStore(wrapper, max_size=s.dek_cache_size), metrics=metrics, breakers=breakers,
        probes={"postgres": db.ping, "redis": state.ping, "kafka": audit_kafka.ping},
    )


def create() -> FastAPI:
    structlog.configure(processors=[structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso"),
                                    structlog.processors.JSONRenderer()])
    return create_app(build_deps(get_settings()))
