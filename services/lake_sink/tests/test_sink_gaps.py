"""Lake-sink gap tests: naming date boundaries/partition formatting, per-partition
topics, content-type + metrics, cross-instance idempotent rewrite, corrupt payload
fail-fast, Parquet field fidelity."""
from __future__ import annotations

import pytest
from fleetsentinel.v1 import telemetry_pb2
from fs_lake_sink.application.sink import LakeSinkService
from fs_lake_sink.domain.batch import events_to_parquet_bytes, parquet_bytes_to_table
from fs_lake_sink.domain.naming import event_date, key_for_event
from fs_lake_sink.models import InMsg

T0 = 1_790_590_000_000  # 2026-09-28 (UTC date part used in keys)


def _ev(vin="V1", ts=T0, seq=1, **kw) -> telemetry_pb2.TelemetryEvent:
    d = {"vin": vin, "ts_ms": ts, "seq": seq, "lat": 12.9716, "lon": 77.5946,
         "speed_kmh": 40.0, "odo_km": 1000.0, "oem": "OEM_A", "ingest_ts_ms": ts + 50}
    d.update(kw)
    e = telemetry_pb2.TelemetryEvent()
    for k, v in d.items():
        if k == "dtc":
            e.dtc.extend(v)
        else:
            setattr(e, k, v)
    return e


def _msg(ev, partition=0, offset=0, topic="telemetry.normalized.v1") -> InMsg:
    return InMsg(topic=topic, partition=partition, offset=offset,
                 key=ev.vin.encode(), value=ev.SerializeToString(), headers={})


class _MemStore:
    def __init__(self):
        self.objects = {}
        self.content_types = {}

    def put(self, key, data, content_type="application/octet-stream"):
        self.objects[key] = data
        self.content_types[key] = content_type


# ---------------------------------------------------------------- naming edges
def test_event_date_rolls_at_utc_midnight():
    day = T0 - (T0 % 86_400_000)
    assert event_date(day - 1) != event_date(day)
    assert event_date(day) == "2026-09-28"


def test_key_varies_by_partition_and_offsets_but_not_event_ts():
    a = key_for_event("t", 3, 100, 199, T0, "OEM_A", "1HGCM82633A004352")
    assert key_for_event("t", 4, 100, 199, T0, "OEM_A", "1HGCM82633A004352") != a
    assert key_for_event("t", 3, 100, 200, T0, "OEM_A", "1HGCM82633A004352") != a
    assert "-00003-000000000100-000000000199.parquet" in a


def test_key_prefix_follows_first_event_date_and_oem():
    k = key_for_event("t", 0, 0, 9, T0, "OEM_B", "1HGCM82633A004352")
    assert k.startswith(f"date={event_date(T0)}/oem=OEM_B/vehicle_shard=")


# ---------------------------------------------------------------- service edges
def test_max_records_one_flushes_every_add():
    store = _MemStore()
    svc = LakeSinkService(store, max_records=1)
    k1 = svc.add(_msg(_ev("V1", T0, 1), offset=0))
    k2 = svc.add(_msg(_ev("V1", T0 + 1, 2), offset=1))
    assert k1 is not None and k2 is not None and k1 != k2
    assert svc.written_keys == [k1, k2]


def test_add_batch_returns_only_flushed_keys_and_sets_content_type():
    store = _MemStore()
    svc = LakeSinkService(store, max_records=10)
    keys = svc.add_batch([_msg(_ev("V1", T0 + i, i + 1), offset=i) for i in range(3)])
    assert keys == [] and svc.pending(0) == 3  # buffered, nothing flushed yet
    keys = svc.flush_all()
    assert len(keys) == 1 and store.content_types[keys[0]] == "application/parquet"
    assert svc.metrics.objects._value.get() == 1
    assert svc.metrics.object_bytes._value.get() == len(store.objects[keys[0]]) > 0


def test_partitions_keep_their_own_topics_and_ranges():
    store = _MemStore()
    svc = LakeSinkService(store, max_records=100)
    svc.add(_msg(_ev("V1"), partition=0, offset=5, topic="telemetry.normalized.v1"))
    svc.add(_msg(_ev("V2"), partition=1, offset=9, topic="telemetry.priority.v1"))
    ka, kb = svc.flush_all()
    assert "telemetry.normalized.v1" in ka and "000000000005-000000000005" in ka
    assert "telemetry.priority.v1" in kb and "000000000009-000000000009" in kb


def test_flush_all_empty_is_noop_and_pending_unknown_is_zero():
    svc = LakeSinkService(_MemStore(), max_records=10)
    assert svc.flush_all() == [] and svc.pending(99) == 0


def test_cross_instance_rewrite_is_byte_identical():
    """Two service instances replaying the same offset range -> same key, same bytes."""
    store = _MemStore()
    msgs = [_msg(_ev("V1", T0 + i, i + 1), offset=50 + i) for i in range(5)]
    svc1 = LakeSinkService(store, max_records=100)
    svc1.add_batch(msgs)
    k1 = svc1.flush(0)
    svc2 = LakeSinkService(store, max_records=100)  # fresh instance after a crash
    svc2.add_batch(msgs)
    k2 = svc2.flush(0)
    assert k1 == k2 and svc1.written_keys == svc2.written_keys == [k1]


def test_corrupt_protobuf_value_skipped_and_counted():
    # Fixed (audit): a poison record must not halt the sink loop — skip + count.
    svc = LakeSinkService(_MemStore(), max_records=100)
    assert svc.add(InMsg("t", 0, 0, b"V1", b"\xff\xff not protobuf \x00\x01", {})) is None
    assert svc.pending(0) == 0


def test_empty_bytes_value_skipped_without_parsing():
    svc = LakeSinkService(_MemStore(), max_records=100)
    assert svc.add(InMsg("t", 0, 0, b"V1", b"", {})) is None
    assert svc.pending(0) == 0


# ---------------------------------------------------------------- parquet fidelity
def test_parquet_preserves_dtc_fuel_ignition_sw():
    evs = [_ev("V1", T0, 1, dtc=["P0300", "U0121"], fuel_pct=63.5, soc_pct=41.0,
               ignition=False, sw_version="nimbus/5.2")]
    rows = parquet_bytes_to_table(events_to_parquet_bytes(evs)).to_pylist()
    assert len(rows) == 1
    r = rows[0]
    assert sorted(r["dtc"]) == ["P0300", "U0121"]
    assert r["fuel_pct"] == pytest.approx(63.5) and r["soc_pct"] == pytest.approx(41.0)
    assert r["ignition"] is False and r["sw_version"] == "nimbus/5.2"
    assert r["oem"] == "OEM_A" and r["ingest_ts_ms"] == T0 + 50


def test_parquet_empty_dtc_and_missing_optionals():
    rows = parquet_bytes_to_table(events_to_parquet_bytes([_ev("V9")])).to_pylist()
    assert rows[0]["dtc"] == [] and rows[0]["soc_pct"] is None and rows[0]["fuel_pct"] is None
