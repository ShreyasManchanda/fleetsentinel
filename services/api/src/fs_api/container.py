"""Dependency container wired by main.py (real adapters) or tests (in-memory fakes)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from fscore.crypto import SubjectKeyStore

from .config import Settings
from .domain.audit import AuditPublisher
from .domain.breaker import CircuitBreaker
from .domain.ratelimit import RateLimiter
from .metrics import Metrics
from .ports import (
    AgentClient,
    AlertFeed,
    IdempotencyStore,
    MappingPublisher,
    QualityStore,
    ReportRunner,
    StateStore,
    TenantDb,
    TwinStore,
)
from .security.jwt_auth import JwtValidator


@dataclass
class Deps:
    settings: Settings
    validator: JwtValidator
    db: TenantDb
    state: StateStore
    twins: TwinStore
    alert_feed: AlertFeed
    mapping_pub: MappingPublisher
    agent: AgentClient
    reports: ReportRunner
    quality: QualityStore
    idem: IdempotencyStore
    limiter: RateLimiter
    audit: AuditPublisher
    keys: SubjectKeyStore
    metrics: Metrics = field(default_factory=Metrics)
    probes: dict[str, Callable[[], Awaitable[Any]]] = field(default_factory=dict)
    breakers: dict[str, CircuitBreaker] = field(default_factory=dict)
