"""Prometheus metrics (gateway_* names match the spec pipeline dashboard)."""
from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class GatewayMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        r = registry or CollectorRegistry()
        self.registry = r
        self.queue_depth = Gauge("gateway_queue_depth", "Batches waiting in the bounded queue", registry=r)
        self.backpressure_seconds = Counter("gateway_backpressure_seconds_total",
                                            "Seconds the MQTT reader was blocked because the queue was full", registry=r)
        self.received = Counter("gateway_batches_received_total", "MQTT batches accepted into the queue", ["kind"], registry=r)
        self.produced = Counter("gateway_batches_produced_total", "Batches delivered to Kafka", ["topic"], registry=r)
        self.rejected = Counter("gateway_batches_rejected_total", "Envelope rejects (sent to DLQ)", ["reason"], registry=r)
        self.duplicates = Counter("gateway_batches_duplicate_total", "Retried batches dropped by exact dedup", registry=r)
        self.bloom_negative = Counter("gateway_dedup_bloom_negative_total", "Batches proven new by the Bloom filter", registry=r)
        self.bloom_maybe = Counter("gateway_dedup_bloom_maybe_total", "Bloom maybe-hits confirmed against the exact store", registry=r)
        self.store_errors = Counter("gateway_dedup_store_errors_total", "Exact store failures (dedup failed open)", registry=r)
        self.spilled = Counter("gateway_spilled_batches_total", "Batches written to the on-disk spill buffer", registry=r)
        self.replayed = Counter("gateway_spill_replayed_batches_total", "Spilled batches replayed to Kafka", registry=r)
        self.spill_bytes = Gauge("gateway_spill_bytes", "Bytes pending in the spill buffer", registry=r)
        self.batch_seconds = Histogram("gateway_batch_seconds", "Worker time per batch of MQTT messages", registry=r,
                                       buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 5))