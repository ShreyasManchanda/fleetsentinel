"""Normaliser core: raw OEM batch -> canonical Protobuf TelemetryEvent messages, DLQ records, drift alerts.

Pure (no Kafka): takes InMsg, returns OutMsg. The Kafka loop lives in adapters/kafka_io.py.
"""
from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Sequence
from typing import Any

import orjson
import structlog
from fleetsentinel.v1 import alert_pb2
from fscore import contracts as C
from fscore.mapping_engine import _walk, to_proto

from .drift import DriftEvent, DriftMonitor
from .encryptor import Encryptor
from .metrics import NormalizerMetrics
from .models import InMsg, OutMsg
from .registry import Entry, MappingRegistry

log = structlog.get_logger("fs_normalizer.core")

SYSTEM_TENANT = "00000000-0000-0000-0000-000000000000"   # platform-level alerts (no vehicle / tenant)
_SW_KEYS = ("schema", "fw", "sw", "sw_version", "version")


def _h(headers: dict[str, bytes], name: str) -> str | None:
    v = headers.get(name)
    return v.decode(errors="replace") if v is not None else None


def _sw_from_payload(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for k in _SW_KEYS:
            v = payload.get(k)
            if isinstance(v, str) and v:
                return v[:64]
    return None


class NormalizerCore:
    def __init__(
        self,
        registry: MappingRegistry,
        encryptor: Encryptor,
        drift: DriftMonitor,
        out_topic: str = C.TELEMETRY_NORMALIZED,
        dlq_topic: str = C.DLQ_INGEST,
        alerts_topic: str = C.ALERTS,
        metrics: NormalizerMetrics | None = None,
        use_fast_path: bool = True,
        clock: Callable[[], int] = lambda: int(time.time() * 1000),
    ) -> None:
        self.registry, self.encryptor, self.drift = registry, encryptor, drift
        self.out_topic, self.dlq_topic, self.alerts_topic = out_topic, dlq_topic, alerts_topic
        self.metrics = metrics or NormalizerMetrics()
        self.use_fast_path = use_fast_path
        self.clock = clock
        self._seen_unknown: set[tuple[str, tuple[str, ...]]] = set()

    # ------------------------------------------------------------------ public
    def process(self, msgs: Sequence[InMsg]) -> list[OutMsg]:
        t0 = time.perf_counter()
        now = self.clock()
        out: list[OutMsg] = []
        for msg in msgs:
            self._one(msg, now, out)
        self.metrics.batch_seconds.observe(time.perf_counter() - t0)
        return out

    # ------------------------------------------------------------------ internals
    def _one(self, msg: InMsg, now: int, out: list[OutMsg]) -> None:
        hdr = msg.headers
        oem = _h(hdr, C.H_OEM) or (msg.key.decode(errors="replace").split(":")[0] if msg.key else "UNKNOWN")
        payload_bytes = msg.value or b""
        received = int(_h(hdr, C.H_RECEIVED_TS_MS) or 0) or msg.timestamp_ms or now
        entry = self.registry.get(oem)
        if entry is None:
            self._dlq(out, oem, C.ERR_MAPPING_MISSING, "no active mapping", payload_bytes, hdr, None, "batch")
            try:
                sw = _sw_from_payload(orjson.loads(payload_bytes))
            except orjson.JSONDecodeError:
                sw = None
            self._account(oem, 0, 1, {C.ERR_MAPPING_MISSING: 1}, now, sw, None, [], out, hdr)
            return
        try:
            payload = orjson.loads(payload_bytes)
        except orjson.JSONDecodeError as e:
            self._dlq(out, oem, C.ERR_BAD_ENVELOPE, f"not JSON: {e}", payload_bytes, hdr, entry.version, "batch")
            self._account(oem, 0, 1, {C.ERR_BAD_ENVELOPE: 1}, now, None, entry.version, [], out, hdr)
            return
        engine = entry.fast if self.use_fast_path else entry.mapping
        res = engine.apply(payload, now)
        batch_id = res.batch_id or _h(hdr, C.H_BATCH_ID) or ""
        self._emit_ok(out, res.ok, entry, oem, batch_id, received, hdr)
        reasons: Counter[str] = Counter()
        for f in res.failed:
            reasons[f.reason] += 1
            raw = payload_bytes if f.raw is None else orjson.dumps(f.raw)
            self._dlq(out, oem, f.reason, f.detail, raw, hdr, entry.version, "batch" if f.raw is None else "record",
                      batch_id)
        sw = (res.ok[0].get("sw_version") if res.ok else None) or _sw_from_payload(payload)
        self._account(oem, len(res.ok), len(res.failed), dict(reasons), now, sw, entry.version,
                      self._unknown(entry, payload), out, hdr)

    def _emit_ok(self, out: list[OutMsg], recs: list[dict], entry: Entry, oem: str, batch_id: str, received: int,
                 hdr: dict[str, bytes]) -> None:
        if not recs:
            return
        refs = [str(r.pop("driver_ref", "") or "") for r in recs]
        distinct = list(dict.fromkeys(r for r in refs if r))
        enc = dict(zip(distinct, self.encryptor.encrypt_many(distinct, oem), strict=True)) if distinct else {}
        headers = {C.H_OEM: oem.encode(), C.H_BATCH_ID: batch_id.encode(), C.H_MAPPING_VERSION: str(entry.version).encode(),
                   C.H_RECEIVED_TS_MS: str(received).encode()}
        tp = hdr.get(C.H_TRACEPARENT)
        if tp:
            headers[C.H_TRACEPARENT] = tp
        topic = self.out_topic
        for rec, ref in zip(recs, refs, strict=True):
            value = to_proto(rec, received, enc.get(ref, b"")).SerializeToString()
            out.append(OutMsg(topic, rec["vin"].encode(), value, headers))

    def _dlq(self, out: list[OutMsg], oem: str, reason: str, detail: str, raw: bytes, hdr: dict[str, bytes],
             mapping_version: int | None, kind: str, batch_id: str = "") -> None:
        h = {C.H_OEM: oem.encode(), C.H_ERROR_REASON: reason.encode(), "error_detail": detail[:200].encode(),
             "dlq_kind": kind.encode(), C.H_BATCH_ID: (batch_id or _h(hdr, C.H_BATCH_ID) or "").encode()}
        if mapping_version is not None:
            h[C.H_MAPPING_VERSION] = str(mapping_version).encode()
        if C.H_RECEIVED_TS_MS in hdr:
            h[C.H_RECEIVED_TS_MS] = hdr[C.H_RECEIVED_TS_MS]
        out.append(OutMsg(self.dlq_topic, oem.encode(), raw, h))
        self.metrics.dlq.labels(oem, reason).inc()

    @staticmethod
    def _unknown(entry: Entry, payload: Any) -> list[str]:
        m = entry.mapping
        recs = _walk(payload, m._records_root[1])
        if not isinstance(recs, list) or not recs:
            return []
        if m._columns:
            cols = _walk(payload, m._columns[1])
            keys = {c for c in cols if isinstance(c, str)} if isinstance(cols, list) else set()
        else:
            keys = set(recs[0]) if isinstance(recs[0], dict) else set()
        return sorted(keys - entry.known_keys)

    def _account(self, oem: str, ok: int, failed: int, reasons: dict[str, int], now: int, sw: str | None,
                 version: int | None, unknown: list[str], out: list[OutMsg], hdr: dict[str, bytes]) -> None:
        mt = self.metrics
        if ok:
            mt.records.labels(oem, "ok").inc(ok)
        if failed:
            mt.records.labels(oem, "failed").inc(failed)
        if version is not None:
            mt.mappings_loaded.labels(oem).set(version)
        if unknown:
            mt.unknown_fields.labels(oem).inc()
            key = (oem, tuple(unknown))
            if key not in self._seen_unknown:
                self._seen_unknown.add(key)
                log.warning("unknown_source_fields", oem=oem, fields=unknown, mapping_version=version)
        ev = self.drift.observe(oem, ok, failed, now, reasons, unknown, sw, version)
        _, _, ratio = self.drift.ratio(oem, now)
        mt.drift_ratio.labels(oem).set(ratio)
        if ev is not None:
            mt.drift_alerts.labels(oem).inc()
            log.warning("schema_drift", oem=oem, ratio=round(ev.ratio, 3), failed=ev.failed, total=ev.total,
                        reasons=ev.reasons, sw_version=ev.sw_version)
            out.append(OutMsg(self.alerts_topic, oem.encode(), self.drift_alert(ev, now, hdr).SerializeToString(),
                              {C.H_OEM: oem.encode()}))

    @staticmethod
    def drift_alert(ev: DriftEvent, now: int, hdr: dict[str, bytes] | None = None) -> alert_pb2.Alert:
        tp = (hdr or {}).get(C.H_TRACEPARENT, b"").decode()
        parts = tp.split("-")
        trace_id = parts[1] if len(parts) >= 3 else ""
        pct = round(ev.ratio * 100)
        text = f"Schema drift: {ev.oem} {pct}% of records failing ({ev.failed}/{ev.total})"
        if ev.sw_version:
            text += f", sw_version {ev.sw_version}"
        a = alert_pb2.Alert(
            alert_id=C.alert_id(ev.oem, "SCHEMA_DRIFT", ev.window_start_ms), tenant_id=SYSTEM_TENANT, vin="",
            rule=alert_pb2.RULE_SCHEMA_DRIFT,
            severity=alert_pb2.CRITICAL if ev.ratio >= 0.9 else alert_pb2.WARNING,
            window_start_ms=ev.window_start_ms, event_ts_ms=now, detected_ts_ms=now, ingest_ts_ms=now,
            summary=text, trace_id=trace_id,
        )
        a.details["oem"] = ev.oem
        a.details["failure_ratio"] = f"{ev.ratio:.3f}"
        a.details["failed"] = str(ev.failed)
        a.details["total"] = str(ev.total)
        a.details["reasons"] = ",".join(f"{k}={v}" for k, v in ev.reasons.items())
        if ev.unknown_fields:
            a.details["unknown_fields"] = ",".join(ev.unknown_fields)
        if ev.sw_version:
            a.details["sw_version"] = ev.sw_version
        if ev.mapping_version is not None:
            a.details["mapping_version"] = str(ev.mapping_version)
        return a