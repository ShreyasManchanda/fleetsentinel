"""Prometheus metrics (names follow spec section 14.5 conventions)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class ProcessorMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        r = registry or CollectorRegistry()
        self.registry = r
        self.alerts = Counter("processor_alerts_total", "Alerts emitted", ["lane", "rule"], registry=r)
        self.suppressed = Counter(
            "processor_alert_suppressed_total", "Alert repeats folded into occurrences", ["lane", "rule"],
            registry=r,
        )
        self.duplicates = Counter("processor_duplicates_total", "Duplicate records dropped", ["lane"], registry=r)
        self.late_events = Counter("late_events_total", "Events past allowed lateness (still forwarded)", registry=r)
        self.dlq = Counter("processor_dlq_total", "Records sent to the DLQ", ["reason"], registry=r)
        self.model_fallback = Gauge("model_fallback_active", "1 when rule-baseline scoring is active", registry=r)
        self.checkpoints = Counter("processor_checkpoints_total", "Per-partition checkpoints written", registry=r)
        self.priority_latency = Histogram(
            "critical_alert_latency_seconds", "Event time to detection for priority alerts", registry=r,
            buckets=(0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
        )
        self.batch_seconds = Histogram(
            "processor_batch_seconds", "Wall time per Kafka poll batch", ["lane"], registry=r,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
        )
        self.model_fallback.set(0)
