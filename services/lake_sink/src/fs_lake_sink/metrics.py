"""Prometheus metrics."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Histogram


class LakeSinkMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        r = registry or CollectorRegistry()
        self.registry = r
        self.records = Counter("lake_records_total", "Normalized events buffered", registry=r)
        self.objects = Counter("lake_objects_written_total", "Parquet objects PUT", registry=r)
        self.object_bytes = Counter("lake_object_bytes_total", "Parquet bytes written", registry=r)
        self.batch_seconds = Histogram(
            "lake_batch_seconds", "Wall time per Kafka poll batch", registry=r,
            buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
        )
