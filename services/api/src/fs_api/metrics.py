"""Prometheus metrics (own registry per app so tests can build many apps)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

BREAKER_STATES = {"closed": 0, "half_open": 1, "open": 2}


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        self.requests = Counter("fs_api_requests_total", "HTTP requests", ["method", "route", "status"], registry=self.registry)
        self.latency = Histogram("fs_api_request_seconds", "HTTP request latency", ["method", "route"], registry=self.registry,
                                 buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.5, 1, 2, 5))
        self.rate_limited = Counter("fs_api_rate_limited_total", "429 responses", ["scope"], registry=self.registry)
        self.denied = Counter("fs_api_denied_total", "401/403 responses", ["reason"], registry=self.registry)
        self.audit_events = Counter("fs_api_audit_events_total", "audit events emitted", ["decision"], registry=self.registry)
        self.audit_buffered = Gauge("fs_api_audit_buffered", "audit events waiting for Kafka", registry=self.registry)
        self.breaker = Gauge("fs_api_circuit_breaker_state", "0 closed, 1 half-open, 2 open", ["name"], registry=self.registry)
        self.ws_connections = Gauge("fs_api_ws_connections", "open WebSocket streams", registry=self.registry)
        self.idempotent_replays = Counter("fs_api_idempotent_replays_total", "replayed POSTs", registry=self.registry)

    def render(self) -> bytes:
        return generate_latest(self.registry)
