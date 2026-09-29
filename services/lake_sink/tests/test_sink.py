"""LakeSinkService: offset-named idempotent objects, batching, Parquet round-trip."""
from __future__ import annotations

import pytest
from fleetsentinel.v1 import telemetry_pb2
from fs_lake_sink.application.sink import LakeSinkService
from fs_lake_sink.domain.batch import events_to_parquet_bytes, events_to_table, parquet_bytes_to_table
from fs_lake_sink.domain.naming import event_date, hive_prefix, key_for_event, object_key
from fs_lake_sink.models import InMsg

T0 = 1_790_590_000_000  # 2026-09-28 (UTC date part used in keys)


def _ev(vin="V1", ts=T0, seq=1, soc=None) -> telemetry_pb2.TelemetryEvent:
    e = telemetry_pb2.TelemetryEvent(
        vin=vin, ts_ms=ts, seq=seq, lat=12.9716, lon=77.5946, speed_kmh=40.0,
        odo_km=1000.0, oem="OEM_A", ingest_ts_ms=ts + 50)
    if soc is not None:
        e.soc_pct = soc
    return e


def _msg(ev, partition=0, offset=0) -> InMsg:
    return InMsg(topic="telemetry.normalized.v1", partition=partition, offset=offset,
                 key=ev.vin.encode(), value=ev.SerializeToString(), headers={})


class _MemStore:
    def __init__(self):
        self.objects = {}

    def put(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data


def test_naming_is_deterministic_and_hive_partitioned():
    k1 = key_for_event("telemetry.normalized.v1", 3, 100, 199, T0, "OEM_A", "1HGCM82633A004352")
    k2 = key_for_event("telemetry.normalized.v1", 3, 100, 199, T0 + 999, "OEM_A", "1HGCM82633A004352")
    assert k1 == k2  # same offset range => same key (idempotent rewrite)
    assert k1.startswith(f"date={event_date(T0)}/oem=OEM_A/vehicle_shard=")
    assert k1.endswith("telemetry.normalized.v1-00003-000000000100-000000000199.parquet")
    assert hive_prefix("2026-09-28", "OEM_B", 7) == "date=2026-09-28/oem=OEM_B/vehicle_shard=7"
    assert object_key("t", 0, 0, 0, "2026-09-28", "OEM_A", 0).count("/") == 3


def test_parquet_roundtrip_preserves_fields():
    evs = [_ev("V1", T0, 1, soc=41.5), _ev("V2", T0 + 1000, 2)]
    tbl = events_to_table(evs)
    assert tbl.num_rows == 2
    back = parquet_bytes_to_table(events_to_parquet_bytes(evs))
    assert back.column("vin").to_pylist() == ["V1", "V2"]
    rows = back.to_pylist()
    assert rows[0]["soc_pct"] == pytest.approx(41.5) and rows[1]["soc_pct"] is None
    assert rows[0]["evt"] == int(evs[0].evt)


def test_flush_on_max_records_and_manual_flush():
    store = _MemStore()
    svc = LakeSinkService(store, max_records=3)
    assert svc.add(_msg(_ev("V1", T0, 1), offset=10)) is None
    assert svc.add(_msg(_ev("V1", T0 + 1, 2), offset=11)) is None
    key = svc.add(_msg(_ev("V1", T0 + 2, 3), offset=12))
    assert key is not None and key in store.objects and svc.pending(0) == 0
    assert "000000000010-000000000012" in key
    assert parquet_bytes_to_table(store.objects[key]).num_rows == 3


def test_rewrite_after_crash_is_idempotent():
    """Same offset range flushed twice => same key, byte-identical object (atomic replace)."""
    store = _MemStore()
    svc = LakeSinkService(store, max_records=100)
    msgs = [_msg(_ev("V1", T0 + i, i + 1), offset=50 + i) for i in range(5)]
    svc.add_batch(msgs)
    k1 = svc.flush(0)
    blob1 = store.objects[k1]
    svc.add_batch(msgs)  # redelivery after a crash before the Kafka commit
    k2 = svc.flush(0)
    assert k1 == k2 and store.objects[k2] == blob1 and len(store.objects) == 1


def test_empty_values_skipped_and_partitions_independent():
    store = _MemStore()
    svc = LakeSinkService(store, max_records=100)
    assert svc.add(InMsg("t", 0, 0, None, None, {})) is None
    svc.add(_msg(_ev("V1"), partition=0, offset=0))
    svc.add(_msg(_ev("V2"), partition=1, offset=7))
    ka, kb = svc.flush_all()
    assert ka != kb and svc.pending(0) == 0 and svc.pending(1) == 0
    assert svc.flush(0) is None  # flushing an empty buffer is a no-op


def test_bad_max_records_rejected():
    with pytest.raises(ValueError):
        LakeSinkService(_MemStore(), max_records=0)
