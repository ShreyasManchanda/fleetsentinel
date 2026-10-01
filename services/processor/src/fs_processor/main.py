"""Process entrypoint: ``FS_LANE=bulk|priority python -m fs_processor.main``.

Poll Kafka -> service.handle_batch -> produce outputs -> checkpoint to Redis ->
commit offsets. Excluded from the unit-coverage gate (integration-tested).
"""
from __future__ import annotations

import logging
import os
import sys

import structlog
from fleetsentinel.v1 import alert_pb2, telemetry_pb2

from .adapters.kafka_io import KafkaSink, make_consumer, to_in
from .adapters.stores import MongoTwinStore, PostgresAlertStore, RedisCheckpointStore
from .application.bulk_service import BulkConfig, BulkRunner, BulkService
from .application.priority_service import PriorityConfig, PriorityService
from .models import OutMsg


def configure_logging() -> None:
    logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
    structlog.configure(
        processors=[structlog.processors.TimeStamper(fmt="iso"), structlog.processors.JSONRenderer()],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    )


def _tenant_resolver(pg_dsn: str):
    """VIN -> tenant_id from the vehicle table (fs_worker bypasses RLS); cached. Unknown VINs fall back to 'unknown'."""
    import psycopg

    conn = psycopg.connect(pg_dsn, autocommit=True)
    cache: dict[str, str] = {}

    def resolve(vin: str) -> str:
        t = cache.get(vin)
        if t is None:
            row = conn.execute("SELECT tenant_id::text FROM vehicle WHERE vin = %s", (vin,)).fetchone()
            t = cache[vin] = row[0] if row else "unknown"
        return t

    return resolve


def _commit(consumer) -> None:
    from confluent_kafka import KafkaError, KafkaException

    try:
        consumer.commit(asynchronous=False)
    except KafkaException as e:
        if e.args[0].code() != KafkaError._NO_OFFSET:  # noqa: SLF001 - nothing consumed yet on this assignment
            raise


def _alert_rows(alerts: list[OutMsg]) -> list[dict]:
    rows = []
    for m in alerts:
        a = alert_pb2.Alert()
        a.ParseFromString(m.value)
        if a.tenant_id == "unknown":  # VIN not in the vehicle table: cannot persist (tenant_id is a UUID FK)
            continue
        rule = alert_pb2.RuleCode.Name(a.rule)
        if rule.startswith("RULE_"):
            rule = rule[len("RULE_"):]
        sev = alert_pb2.Severity.Name(a.severity)
        rows.append({"alert_id": a.alert_id, "tenant_id": a.tenant_id, "vin": a.vin,
                     "rule": rule, "severity": sev, "impact_usd": a.impact_usd})
    return rows


def _run_bulk(kafka: str, redis_url: str, pg_dsn: str, mongo_uri: str) -> None:
    log = structlog.get_logger("fs_processor.bulk")
    consumer = make_consumer(kafka, "processor-bulk", ["telemetry.normalized.v1"])
    sink, alerts = KafkaSink(kafka), PostgresAlertStore(pg_dsn)
    ckpt = RedisCheckpointStore(redis_url)
    twins = MongoTwinStore(mongo_uri)
    svc = BulkService(partition=0, config=BulkConfig(), tenant_of=_tenant_resolver(pg_dsn))
    runner = BulkRunner(svc, ckpt, _Committer(consumer))
    runner.restore()
    log.info("bulk_loop_start")
    while True:
        batch = consumer.consume(num_messages=500, timeout=1.0)
        msgs = []
        for m in batch:
            if m is None or m.error():
                continue
            ev = telemetry_pb2.TelemetryEvent()
            try:
                ev.ParseFromString(m.value())
            except Exception as e:  # noqa: BLE001 - corrupt protobuf goes to DLQ downstream, not here
                log.warning("bad_protobuf", error=str(e))
                continue
            msgs.append((to_in(m), ev))
        if not msgs:
            continue
        res = runner.handle_batch(msgs)
        for o in (*res.alerts, *res.states):
            sink.produce(o)
        sink.flush()
        alerts.insert_alerts(_alert_rows(res.alerts))
        for s in res.states:
            ckpt.put_state(s.key.decode(), s.value)
        if res.states:
            twins.upsert(s.key.decode(), {"vin": s.key.decode()})
        _commit(consumer)


class _Committer:
    def __init__(self, consumer) -> None:
        self._c = consumer

    def commit(self, partition: int, offset: int) -> None:
        self._c.commit(asynchronous=False)


def _run_priority(kafka: str, pg_dsn: str) -> None:
    log = structlog.get_logger("fs_processor.priority")
    consumer = make_consumer(kafka, "processor-priority", ["telemetry.priority.v1"])
    sink, alerts = KafkaSink(kafka), PostgresAlertStore(pg_dsn)
    svc = PriorityService(config=PriorityConfig(), tenant_of=_tenant_resolver(pg_dsn))
    log.info("priority_loop_start")
    while True:
        batch = consumer.consume(num_messages=200, timeout=1.0)
        msgs = []
        for m in batch:
            if m is None or m.error():
                continue
            ev = telemetry_pb2.TelemetryEvent()
            try:
                ev.ParseFromString(m.value())
            except Exception as e:  # noqa: BLE001 - corrupt protobuf is skipped, not fatal
                log.warning("bad_protobuf", error=str(e))
                continue
            msgs.append((to_in(m), ev))
        if not msgs:
            continue
        out = svc.handle_batch(msgs)
        for o in out:
            sink.produce(o)
        sink.flush()
        alerts.insert_alerts(_alert_rows([o for o in out if o.topic == "alerts.v1"]))
        _commit(consumer)


def main() -> None:
    configure_logging()
    log = structlog.get_logger("fs_processor.main")
    lane = os.environ.get("FS_LANE", "bulk")
    kafka = os.environ.get("FS_KAFKA", "localhost:19092")
    log.info("processor_start", lane=lane, kafka=kafka)
    if lane == "priority":
        _run_priority(kafka, os.environ.get("FS_PG_DSN", "postgresql://fs_worker:fs_worker_dev@localhost:5432/fleetsentinel"))
    else:
        _run_bulk(kafka, os.environ.get("FS_REDIS", "redis://localhost:6379/0"),
                  os.environ.get("FS_PG_DSN", "postgresql://fs_worker:fs_worker_dev@localhost:5432/fleetsentinel"),
                  os.environ.get("FS_MONGO", "mongodb://localhost:27017"))


if __name__ == "__main__":
    main()
