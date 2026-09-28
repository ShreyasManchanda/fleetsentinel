"""Process entrypoint: ``python -m fs_normalizer.main`` (FS_NORMALIZER_LANE=bulk|priority)."""
from __future__ import annotations

import logging
import signal
import sys

import structlog
from prometheus_client import start_http_server

from .core import NormalizerCore
from .drift import DriftMonitor
from .encryptor import AesGcmDevEncryptor, CachingEncryptor
from .metrics import NormalizerMetrics
from .registry import MappingRegistry
from .runner import Runner
from .settings import Settings


def configure_logging() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso", utc=True),
                    structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO), cache_logger_on_first_use=True,
    )


def main() -> None:
    from .adapters.kafka_io import KafkaSink, run

    configure_logging()
    log = structlog.get_logger("fs_normalizer")
    s = Settings().resolved()
    metrics = NormalizerMetrics()
    registry = MappingRegistry()
    log.info("mappings_loaded", loaded=registry.load_dir(s.mappings_dir, s.startup_mappings))
    enc = AesGcmDevEncryptor.from_b64(s.driver_key_b64, s.driver_key_id)
    if enc.is_fallback_key:
        log.warning("dev_fallback_encryption_key_in_use")
    core = NormalizerCore(registry, CachingEncryptor(enc), DriftMonitor(s.drift_window_s, s.drift_min_events,
                          s.drift_threshold, s.drift_cooldown_s), s.out_topic, s.dlq_topic, s.alerts_topic, metrics,
                          s.use_fast_path)
    runner = Runner(core, registry, KafkaSink(s.kafka_bootstrap), metrics)
    stop = {"flag": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(flag=True))
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))
    start_http_server(s.metrics_port, registry=metrics.registry)
    log.info("normalizer_start", lane=s.lane, group=s.group_id, in_topic=s.in_topic, out_topic=s.out_topic)
    run(runner, s.kafka_bootstrap, s.group_id, s.in_topic, s.config_topic, s.batch_max_msgs, s.poll_timeout_s,
        lambda: stop["flag"])


if __name__ == "__main__":
    main()