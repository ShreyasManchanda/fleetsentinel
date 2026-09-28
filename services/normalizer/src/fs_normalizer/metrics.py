"""Prometheus metrics (names follow spec section 14.5 conventions)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class NormalizerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        r = registry or CollectorRegistry()
        self.registry = r
        self.records = Counter("normalizer_records_total", "Records processed", ["oem", "result"], registry=r)
        self.dlq = Counter("normalizer_dlq_total", "Records sent to the DLQ", ["oem", "reason"], registry=r)
        self.batch_seconds = Histogram(
            "normalizer_batch_seconds", "Wall time to normalise one Kafka poll batch", registry=r,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
        )
        self.drift_ratio = Gauge("normalizer_failure_ratio", "Trailing failure ratio per OEM", ["oem"], registry=r)
        self.drift_alerts = Counter("normalizer_schema_drift_alerts_total", "SCHEMA_DRIFT alerts raised", ["oem"], registry=r)
        self.unknown_fields = Counter("normalizer_unknown_fields_total", "Batches with unmapped source fields", ["oem"], registry=r)
        self.mappings_loaded = Gauge("normalizer_mapping_version", "Active mapping version", ["oem"], registry=r)
        self.mapping_reloads = Counter("normalizer_mapping_reloads_total", "Hot reloads applied", ["oem"], registry=r)
        self.mapping_rejects = Counter("normalizer_mapping_rejects_total", "Invalid mapping documents ignored", registry=r)