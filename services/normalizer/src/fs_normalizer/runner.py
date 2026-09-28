"""Application loop logic (Kafka-free, unit-tested): produce-then-commit batching and config hot reload."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import structlog

from .core import NormalizerCore
from .metrics import NormalizerMetrics
from .models import InMsg, OutMsg
from .registry import MappingRegistry

log = structlog.get_logger("fs_normalizer.runner")


class Sink(Protocol):
    def produce(self, msg: OutMsg) -> None: ...

    def flush(self, timeout_s: float = 30.0) -> int: ...      # returns messages still undelivered


class DeliveryError(RuntimeError):
    """Outputs were not fully delivered; the input offsets must NOT be committed."""


class Runner:
    def __init__(self, core: NormalizerCore, registry: MappingRegistry, sink: Sink, metrics: NormalizerMetrics) -> None:
        self.core, self.registry, self.sink, self.metrics = core, registry, sink, metrics

    def handle_config(self, msgs: Sequence[InMsg]) -> int:
        """Apply config.mappings.v1 records; returns how many changed the active set."""
        changed = 0
        for m in msgs:
            outcome, oem = self.registry.apply_config_message(m.key, m.value)
            if outcome in ("loaded", "retired"):
                changed += 1
                self.metrics.mapping_reloads.labels(oem).inc()
                v = self.registry.get(oem)
                log.info("mapping_hot_reload", oem=oem, outcome=outcome, version=v.version if v else None)
            elif outcome == "rejected":
                self.metrics.mapping_rejects.inc()
        return changed

    def handle_batch(self, msgs: Sequence[InMsg]) -> int:
        """Normalise ``msgs`` and deliver every output. Raises DeliveryError if delivery is incomplete (caller must not
        commit the consumer offsets, giving at-least-once)."""
        if not msgs:
            return 0
        out = self.core.process(msgs)
        for o in out:
            self.sink.produce(o)
        left = self.sink.flush()
        if left:
            raise DeliveryError(f"{left} messages undelivered")
        log.info("batch_done", inputs=len(msgs), outputs=len(out), first_offset=msgs[0].offset, last_offset=msgs[-1].offset,
                 partition=msgs[0].partition)
        return len(out)