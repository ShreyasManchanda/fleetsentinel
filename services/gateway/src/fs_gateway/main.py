"""Entrypoint: ``python -m fs_gateway.main``."""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

import structlog
from fscore.sketches import RotatingBloom
from prometheus_client import start_http_server

from .application.pipeline import GatewayPipeline
from .domain.dedup import BatchDeduper
from .domain.spill import SpillBuffer
from .metrics import GatewayMetrics
from .settings import Settings


def configure_logging() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso", utc=True),
                    structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO), cache_logger_on_first_use=True,
    )


async def amain() -> None:
    from .adapters.kafka_sink import KafkaSink
    from .adapters.mqtt_client import run_subscriber
    from .adapters.redis_store import RedisExactStore

    configure_logging()
    log = structlog.get_logger("fs_gateway")
    s = Settings()
    metrics = GatewayMetrics()
    start_http_server(s.metrics_port, registry=metrics.registry)
    sink = KafkaSink(s.kafka_bootstrap)
    await sink.start()
    deduper = BatchDeduper(RedisExactStore(s.redis_url), RotatingBloom(s.bloom_capacity, 0.01), s.dedup_ttl_s, metrics=metrics)
    spill = SpillBuffer(s.spill_dir, s.spill_max_bytes)
    pipeline = GatewayPipeline(sink, deduper, spill, s.oems(), metrics, s.queue_size, s.batch_size, s.batch_wait_ms / 1000,
                               s.max_payload_bytes, s.traces_sample_bulk)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:       # Windows
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
    log.info("gateway_start", spill_pending=len(spill), mqtt=f"{s.mqtt_host}:{s.mqtt_port}", kafka=s.kafka_bootstrap)
    worker = asyncio.create_task(pipeline.run())
    try:
        await run_subscriber(s, pipeline, stop)
    finally:
        pipeline.stop()
        await worker
        await sink.close()
        spill.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()