"""Topic names, header names and stable namespaces. Mirrors contracts/topics.yaml (a test enforces parity)."""
from __future__ import annotations

import uuid

INGEST_RAW = "ingest.raw.v1"
INGEST_PRIORITY = "ingest.priority.v1"
TELEMETRY_NORMALIZED = "telemetry.normalized.v1"
TELEMETRY_PRIORITY = "telemetry.priority.v1"
VEHICLE_STATE = "vehicle.state.v1"
ALERTS = "alerts.v1"
AUDIT_EVENTS = "audit.events.v1"
DLQ_INGEST = "dlq.ingest.v1"
CONFIG_MAPPINGS = "config.mappings.v1"

ALL_TOPICS = (
    INGEST_RAW, INGEST_PRIORITY, TELEMETRY_NORMALIZED, TELEMETRY_PRIORITY, VEHICLE_STATE,
    ALERTS, AUDIT_EVENTS, DLQ_INGEST, CONFIG_MAPPINGS,
)

# Kafka record header names
H_OEM = "oem"
H_BATCH_ID = "batch_id"
H_RECEIVED_TS_MS = "received_ts_ms"
H_TRACEPARENT = "traceparent"
H_ERROR_REASON = "error_reason"
H_MAPPING_VERSION = "mapping_version"

# DLQ error reasons
ERR_MAPPING_MISSING = "MAPPING_MISSING"
ERR_VALIDATION_FAILED = "VALIDATION_FAILED"
ERR_CLOCK_SKEW = "CLOCK_SKEW"
ERR_BAD_ENVELOPE = "BAD_ENVELOPE"

# MQTT
MQTT_TELEMETRY_TOPIC_FMT = "telemetry/{oem}/{shard}"   # periodic bulk batches -> ingest.raw.v1; shard = murmur2(vin) mod 64
MQTT_EVENT_TOPIC_FMT = "event/{oem}/{shard}"           # OEM-pushed critical events + watchlist telemetry -> ingest.priority.v1
N_SHARDS = 64
N_PARTITIONS_BULK = 64

# Deterministic IDs: alert_id = uuid5(NAMESPACE_FS, f"{vin}|{rule_code}|{window_start_ms}")
NAMESPACE_FS = uuid.UUID("6f1c5c0e-7d0e-4a57-9d6f-2b6f0c1e5a10")


def alert_id(vin: str, rule_code: str, window_start_ms: int) -> str:
    return str(uuid.uuid5(NAMESPACE_FS, f"{vin}|{rule_code}|{window_start_ms}"))


def trip_id(vin: str, start_ts_ms: int) -> str:
    return str(uuid.uuid5(NAMESPACE_FS, f"trip|{vin}|{start_ts_ms}"))


def tenant_id(slug: str) -> str:
    """Deterministic tenant id shared by the Keycloak realm generator and the simulator seed."""
    return str(uuid.uuid5(NAMESPACE_FS, f"tenant|{slug}"))
