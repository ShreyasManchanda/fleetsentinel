"""Bulk lane: dedup, event-time windows, trips/idling, sketches, depot clusters,
VehicleState, ML scoring with fallback, checkpoint-before-commit. Kafka-free."""
from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog
from fleetsentinel.v1 import telemetry_pb2
from fscore import contracts as C
from fscore.algorithms import (
    AntiReplayStore,
    EventTimeWindows,
    IdleDetector,
    RecentSet,
    Sample,
    TariffRow,
    TripDetector,
    TripParams,
    Verdict,
    encode,
    idling_cost,
)
from fscore.algorithms.geo import haversine_km
from fscore.sketches import CountMinSketch, HyperLogLog, TopK

from ..domain.bulk import build_vehicle_state, cluster_depots, is_harsh
from ..domain.geofence import Geofence
from ..domain.lifecycle import AlertLedger
from ..domain.rules import Finding
from ..domain.scoring import Features, RiskScorer
from ..metrics import ProcessorMetrics
from ..models import InMsg, OutMsg
from .checkpoint import b64d, b64e, decode_snapshot, encode_snapshot

log = structlog.get_logger("fs_processor.bulk")

WINDOW_MS = 60_000
LATENESS_MS = 10_000
SKEW_FUTURE_MS = 120_000
CLUSTER_MS = 300_000
MAX_PLAUSIBLE_KMH = 250.0
HARSH_THRESHOLD = 3
IDLE_ALERT_S = 300.0


@dataclass(slots=True)
class BulkConfig:
    tariffs: list[TariffRow] = field(default_factory=lambda: [TariffRow("DIESEL", 0, 24, 1.0, "L")])
    vehicle_energy: dict[str, tuple[str, float]] = field(default_factory=dict)  # vin -> (energy_type, burn/h)
    approved: list[Geofence] = field(default_factory=list)
    min_cluster: int = 3
    harsh_threshold: int = HARSH_THRESHOLD
    idle_alert_s: float = IDLE_ALERT_S
    tenant_default: str = "unknown"
    model_path: str | None = None


@dataclass(slots=True)
class BulkResult:
    alerts: list[OutMsg] = field(default_factory=list)
    states: list[OutMsg] = field(default_factory=list)
    lake: list[bytes] = field(default_factory=list)  # serialized events, incl. late ones
    trips: list[dict] = field(default_factory=list)
    dlq: list[OutMsg] = field(default_factory=list)
    closed_windows: list[tuple] = field(default_factory=list)


class BulkService:
    """Per-partition bulk processor. One instance per assigned partition."""

    def __init__(
        self,
        partition: int = 0,
        config: BulkConfig | None = None,
        ledger: AlertLedger | None = None,
        metrics: ProcessorMetrics | None = None,
        tenant_of: Callable[[str], str] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.partition = partition
        self.config = config or BulkConfig()
        self.metrics = metrics or ProcessorMetrics()
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.ledger = ledger or AlertLedger(clock=self._clock)
        self._tenant_of = tenant_of or (lambda _vin: self.config.tenant_default)
        self.scorer = RiskScorer(
            self.config.model_path, on_fallback=lambda f: self.metrics.model_fallback.set(int(f)))
        self._dedup = AntiReplayStore()
        self._recent = RecentSet()
        self._windows = EventTimeWindows(WINDOW_MS, LATENESS_MS, factory=dict)
        self._trips: dict[str, TripDetector] = {}
        self._idles: dict[str, IdleDetector] = {}
        self._cms = CountMinSketch()
        self._topk = TopK(10, self._cms)
        self._hll: dict[str, HyperLogLog] = {}
        self._harsh_live: dict[tuple[str, int], int] = {}
        self._last_pos: dict[str, tuple[float, float, int]] = {}
        self._last_odo: dict[str, float] = {}
        self._idle_spots: dict[str, tuple[float, float]] = {}
        self._last_cluster_tick: int | None = None
        self.gps_jumps = 0
        self.last_offset = -1

    # ------------------------------------------------------------------ public
    def handle(self, msg: InMsg, ev: telemetry_pb2.TelemetryEvent) -> BulkResult:
        res = BulkResult()
        if ev.ts_ms > self._clock() + SKEW_FUTURE_MS:
            res.dlq.append(self._dlq(msg, C.ERR_CLOCK_SKEW, "event >2min in the future"))
            self.metrics.dlq.labels(C.ERR_CLOCK_SKEW).inc()
            return res
        if not self._dedup_check(ev):
            self.metrics.duplicates.labels("bulk").inc()
            return res
        applied = self._windows.add(ev.vin, ev.ts_ms, lambda agg: self._apply_agg(agg, ev))
        if not applied:
            self.metrics.late_events.inc()
        else:
            self._on_time(ev, res)
        self._housekeeping(ev, res, applied)
        res.lake.append(ev.SerializeToString())
        self.last_offset = msg.offset
        return res

    def handle_batch(self, msgs: list[tuple[InMsg, telemetry_pb2.TelemetryEvent]]) -> BulkResult:
        t0 = time.perf_counter()
        total = BulkResult()
        for msg, ev in msgs:
            one = self.handle(msg, ev)
            total.alerts.extend(one.alerts)
            total.states.extend(one.states)
            total.lake.extend(one.lake)
            total.trips.extend(one.trips)
            total.dlq.extend(one.dlq)
            total.closed_windows.extend(one.closed_windows)
        total.closed_windows.extend(self._drain())
        log.info("bulk_batch", inputs=len(msgs), alerts=len(total.alerts), states=len(total.states),
                 late=self._windows.late_events, partition=self.partition)
        self.metrics.batch_seconds.labels("bulk").observe(time.perf_counter() - t0)
        return total

    # ------------------------------------------------------------------ internals
    def _dedup_check(self, ev: telemetry_pb2.TelemetryEvent) -> bool:
        if ev.seq > 0:
            v = self._dedup.check_and_update(ev.vin, ev.seq)
            return v is Verdict.NEW
        key = (ev.vin, ev.ts_ms, hashlib.blake2b(ev.SerializeToString(), digest_size=16).digest())
        return self._recent.add_if_new(key)

    def _apply_agg(self, agg: dict, ev: telemetry_pb2.TelemetryEvent) -> None:
        agg["n"] = agg.get("n", 0) + 1
        if is_harsh(ev.evt):
            agg["harsh"] = agg.get("harsh", 0) + 1
        for code in ev.dtc:
            agg[f"dtc:{code}"] = agg.get(f"dtc:{code}", 0) + 1

    def _on_time(self, ev: telemetry_pb2.TelemetryEvent, res: BulkResult) -> None:
        for code in ev.dtc:
            self._topk.update(code)
        if is_harsh(ev.evt):
            ws = ev.ts_ms - ev.ts_ms % WINDOW_MS
            k = (ev.vin, ws)
            self._harsh_live[k] = self._harsh_live.get(k, 0) + 1
            if self._harsh_live[k] == self.config.harsh_threshold:
                self._emit(res, ev, Finding(
                    rule="HARSH_DRIVING",
                    summary=f"{ev.vin} harsh-event rate {self._harsh_live[k]}/{WINDOW_MS // 1000}s",
                    details={"harsh_count": str(self._harsh_live[k])}))
        if ev.odo_km < self._last_odo.get(ev.vin, float("-inf")) - 1e-6:
            self._emit(res, ev, Finding(
                rule="ODOMETER_ROLLBACK",
                summary=f"{ev.vin} odometer rollback: {ev.odo_km:.1f} < {self._last_odo[ev.vin]:.1f}",
                details={"odo_km": f"{ev.odo_km:.1f}", "prev_odo_km": f"{self._last_odo[ev.vin]:.1f}"}))
        else:
            self._last_odo[ev.vin] = max(ev.odo_km, self._last_odo.get(ev.vin, float("-inf")))
        trip = self._trip_of(ev.vin).update(Sample(ev.ts_ms, ev.speed_kmh, ev.ignition, ev.lat, ev.lon, ev.odo_km))
        if trip is not None:
            res.trips.append({"trip_id": C.trip_id(trip.vin, trip.start_ts_ms), "vin": trip.vin,
                              "start_ts_ms": trip.start_ts_ms, "end_ts_ms": trip.end_ts_ms,
                              "distance_km": trip.distance_km})
        seg = self._idle_of(ev.vin).update(Sample(ev.ts_ms, ev.speed_kmh, ev.ignition, ev.lat, ev.lon, ev.odo_km))
        if seg is not None and seg.seconds >= self.config.idle_alert_s:
            energy, burn = self.config.vehicle_energy.get(ev.vin, ("DIESEL", 2.0))
            cost = idling_cost(seg.start_ts_ms, seg.end_ts_ms, self.config.tariffs, energy, burn)
            self._emit(res, ev, Finding(
                rule="EXCESSIVE_IDLING",
                summary=f"{ev.vin} idled {seg.seconds / 60:.0f} min costing ${cost.cost_usd:.2f}",
                details={"idle_s": f"{seg.seconds:.0f}", "cost_usd": f"{cost.cost_usd:.2f}"},
                impact_usd=cost.cost_usd))

    def _housekeeping(self, ev: telemetry_pb2.TelemetryEvent, res: BulkResult, applied: bool) -> None:
        lat, lon = self._plausible_pos(ev)
        if ev.ignition and ev.speed_kmh < 1.0:
            self._idle_spots[ev.vin] = (lat, lon)
        elif not ev.ignition:
            self._idle_spots.pop(ev.vin, None)
        cell5 = encode(lat, lon, 5)
        hll = self._hll.get(cell5)
        if hll is None:
            hll = self._hll[cell5] = HyperLogLog()
        hll.add(ev.vin)
        tenant = self._tenant_of(ev.vin)
        feats = Features(dtc_count=len(ev.dtc),
                         harsh_count=self._harsh_live.get((ev.vin, ev.ts_ms - ev.ts_ms % WINDOW_MS), 0))
        high, score, version = self.scorer.is_high_risk(feats, bool(ev.dtc))
        state = build_vehicle_state(
            self._with_pos(ev, lat, lon), tenant, score, self.ledger.open_count(ev.vin))
        headers = {C.H_OEM: ev.oem.encode()}
        res.states.append(OutMsg(C.VEHICLE_STATE, ev.vin.encode(), state.SerializeToString(), headers))
        if high and applied:
            self._emit(res, ev, Finding(
                rule="MAINTENANCE_RISK", summary=f"{ev.vin} maintenance risk {score:.2f} ({version})",
                details={"risk": f"{score:.3f}", "model": version}))
        self._maybe_cluster(ev, res)

    def _plausible_pos(self, ev: telemetry_pb2.TelemetryEvent) -> tuple[float, float]:
        prev = self._last_pos.get(ev.vin)
        self._last_pos[ev.vin] = (ev.lat, ev.lon, ev.ts_ms)
        if prev is None:
            return ev.lat, ev.lon
        dt_h = (ev.ts_ms - prev[2]) / 3_600_000
        if dt_h > 0 and haversine_km(prev[0], prev[1], ev.lat, ev.lon) / dt_h > MAX_PLAUSIBLE_KMH:
            self.gps_jumps += 1
            return prev[0], prev[1]
        return ev.lat, ev.lon

    def _with_pos(self, ev: telemetry_pb2.TelemetryEvent, lat: float, lon: float) -> telemetry_pb2.TelemetryEvent:
        if (lat, lon) == (ev.lat, ev.lon):
            return ev
        fix = telemetry_pb2.TelemetryEvent()
        fix.CopyFrom(ev)
        fix.lat, fix.lon = lat, lon
        return fix

    def _maybe_cluster(self, ev: telemetry_pb2.TelemetryEvent, res: BulkResult) -> None:
        tick = self._windows.watermark - self._windows.watermark % CLUSTER_MS
        if self._windows.watermark < 0:
            return
        if self._last_cluster_tick is None:
            self._last_cluster_tick = tick
            return
        if tick <= self._last_cluster_tick:
            return
        self._last_cluster_tick = tick
        points = [(vin, la, lo) for vin, (la, lo) in self._idle_spots.items()]
        for vins in cluster_depots(points, self.config.approved, self.config.min_cluster):
            rep = vins[0]
            alert_ev = telemetry_pb2.TelemetryEvent()
            alert_ev.CopyFrom(ev)
            self._emit_to(res, rep, alert_ev, Finding(
                rule="UNAPPROVED_DEPOT_CLUSTER",
                summary=f"Unapproved depot cluster of {len(vins)} vehicles near {rep}",
                details={"vehicles": ",".join(vins), "size": str(len(vins))}),
                window_start_ms=tick)

    def _emit(self, res: BulkResult, ev: telemetry_pb2.TelemetryEvent, finding: Finding) -> None:
        self._emit_to(res, ev.vin, ev, finding)

    def _emit_to(self, res: BulkResult, vin: str, ev: telemetry_pb2.TelemetryEvent, finding: Finding,
                 window_start_ms: int | None = None) -> None:
        alert = self.ledger.maybe_emit(
            vin, self._tenant_of(vin), finding, ev.ts_ms, ev.ingest_ts_ms, ev.lat, ev.lon,
            {C.H_OEM: ev.oem.encode()}, window_start_ms)
        if alert is None:
            self.metrics.suppressed.labels("bulk", finding.rule).inc()
            return
        res.alerts.append(OutMsg(C.ALERTS, vin.encode(), alert.SerializeToString(),
                                 {C.H_OEM: ev.oem.encode()}))
        self.metrics.alerts.labels("bulk", finding.rule).inc()

    def _drain(self) -> list[tuple]:
        return self._windows.drain_closed()

    def _dlq(self, msg: InMsg, reason: str, detail: str) -> OutMsg:
        return OutMsg(C.DLQ_INGEST, (msg.key or b"UNKNOWN"),
                      msg.value or b"", {C.H_ERROR_REASON: reason.encode(), "error_detail": detail.encode()})

    def _trip_of(self, vin: str) -> TripDetector:
        t = self._trips.get(vin)
        if t is None:
            t = self._trips[vin] = TripDetector(vin, TripParams(stop_hold_ms=60_000))
        return t

    def _idle_of(self, vin: str) -> IdleDetector:
        d = self._idles.get(vin)
        if d is None:
            d = self._idles[vin] = IdleDetector(vin, min_idle_ms=60_000)
        return d

    # ------------------------------------------------------------------ checkpoint
    def snapshot(self) -> dict:
        return encode_snapshot({
            "partition": self.partition,
            "last_offset": self.last_offset,
            "dedup": self._dedup.snapshot(),
            "recent": [[v, ts, b64e(dg)] for v, ts, dg in self._recent.snapshot()],
            "windows": self._windows.snapshot(),
            "trips": {v: t.snapshot() for v, t in self._trips.items()},
            "idles": {v: {"start": d.snapshot()["start"], "last": d.snapshot()["last"]}
                      for v, d in self._idles.items()},
            "cms": {"table": self._cms.to_bytes(), "w": self._cms.w, "d": self._cms.d, "total": self._cms.total},
            "topk": self._topk.snapshot(),
            "hll": {cell: h.to_bytes() for cell, h in self._hll.items()},
            "ledger": self.ledger.snapshot(),
            "harsh": [[v, ws, n] for (v, ws), n in self._harsh_live.items()],
            "odo": dict(self._last_odo),
            "pos": {v: list(p) for v, p in self._last_pos.items()},
            "spots": {v: list(p) for v, p in self._idle_spots.items()},
            "cluster_tick": self._last_cluster_tick,
            "gps_jumps": self.gps_jumps,
        })

    def restore(self, data: dict) -> None:
        d = decode_snapshot(data)
        self.partition = d.get("partition", self.partition)
        self.last_offset = d.get("last_offset", -1)
        self._dedup = AntiReplayStore.restore(d.get("dedup", {}))
        self._recent = RecentSet.restore([(v, ts, b64d(dg)) for v, ts, dg in d.get("recent", [])])
        self._windows = EventTimeWindows(WINDOW_MS, LATENESS_MS, factory=dict)
        self._windows.restore(d["windows"])
        self._trips = {v: TripDetector.restore(v, s) for v, s in d.get("trips", {}).items()}
        self._idles = {}
        for v, s in d.get("idles", {}).items():
            det = IdleDetector(v, min_idle_ms=60_000)
            det.restore_state(s)
            self._idles[v] = det
        cms_d = d["cms"]
        self._cms = CountMinSketch.from_bytes(cms_d["table"], cms_d["w"], cms_d["d"], cms_d.get("total", 0))
        self._topk = TopK(10, self._cms)
        self._topk.restore_items(d.get("topk", {}))
        self._hll = {cell: HyperLogLog.from_bytes(blob) for cell, blob in d.get("hll", {}).items()}
        self.ledger.restore(d.get("ledger", {}))
        self._harsh_live = {(v, ws): n for v, ws, n in d.get("harsh", [])}
        self._last_odo = {v: float(o) for v, o in d.get("odo", {}).items()}
        self._last_pos = {v: tuple(p) for v, p in d.get("pos", {}).items()}
        self._idle_spots = {v: tuple(p) for v, p in d.get("spots", {}).items()}
        self._last_cluster_tick = d.get("cluster_tick")
        self.gps_jumps = d.get("gps_jumps", 0)


class CheckpointStore:
    """Protocol for atomic per-partition checkpoints (Redis MULTI/EXEC in the adapter)."""

    def save(self, partition: int, state: dict) -> None:
        raise NotImplementedError

    def load(self, partition: int) -> dict | None:
        raise NotImplementedError


class OffsetCommitter:
    """Protocol for Kafka offset commits (called only after a successful checkpoint)."""

    def commit(self, partition: int, offset: int) -> None:
        raise NotImplementedError


class BulkRunner:
    """Batch loop: process -> checkpoint to Redis (atomic) -> commit Kafka offset."""

    def __init__(self, service: BulkService, store: CheckpointStore, committer: OffsetCommitter,
                 metrics: ProcessorMetrics | None = None) -> None:
        self.service, self.store, self.committer = service, store, committer
        self.metrics = metrics or service.metrics

    def handle_batch(self, msgs: list[tuple[InMsg, telemetry_pb2.TelemetryEvent]]) -> BulkResult:
        if not msgs:
            return BulkResult()
        res = self.service.handle_batch(msgs)
        self.store.save(self.service.partition, self.service.snapshot())
        self.metrics.checkpoints.inc()
        self.committer.commit(self.service.partition, msgs[-1][0].offset)
        log.info("bulk_checkpoint", partition=self.service.partition, offset=msgs[-1][0].offset,
                 alerts=len(res.alerts))
        return res

    def restore(self) -> None:
        data = self.store.load(self.service.partition)
        if data is not None:
            self.service.restore(data)
            log.info("bulk_restored", partition=self.service.partition, offset=self.service.last_offset)
