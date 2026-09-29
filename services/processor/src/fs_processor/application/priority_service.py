"""Priority lane: per-event rules + GOING_DARK via the hierarchical timing wheel.

The wheel is driven by the EVENT-time watermark (max event time seen), never wall clock:
a vehicle goes dark when ``watermark - last_seen >= dark_timeout``. Each event refreshes
its deadline; advancing the watermark fires overdue deadlines within ~1 tick.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import structlog
from fleetsentinel.v1 import telemetry_pb2
from fscore import contracts as C
from fscore.algorithms.timing_wheel import TimingWheel

from ..domain.geofence import Geofence
from ..domain.lifecycle import AlertLedger
from ..domain.rules import ChargerConfig, Finding, evaluate_priority
from ..metrics import ProcessorMetrics
from ..models import InMsg, OutMsg

log = structlog.get_logger("fs_processor.priority")

DARK_TIMEOUT_MS = 900_000  # 15 min without data => GOING_DARK


@dataclass(slots=True)
class PriorityConfig:
    watchlist: frozenset[str] = frozenset()
    approved: list[Geofence] = field(default_factory=list)
    charger_cfg: ChargerConfig | None = None
    dark_timeout_ms: int = DARK_TIMEOUT_MS
    tenant_default: str = "unknown"


class PriorityService:
    def __init__(
        self,
        config: PriorityConfig | None = None,
        ledger: AlertLedger | None = None,
        metrics: ProcessorMetrics | None = None,
        tenant_of: Callable[[str], str] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.config = config or PriorityConfig()
        self.ledger = ledger or AlertLedger(clock=clock)
        self.metrics = metrics or ProcessorMetrics()
        self._tenant_of = tenant_of or (lambda _vin: self.config.tenant_default)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._wheel = TimingWheel(tick_ms=1000)
        self._last_seen: dict[str, int] = {}
        self._watermark = -1

    # ------------------------------------------------------------------ public
    def handle(self, ev: telemetry_pb2.TelemetryEvent, headers: dict[str, bytes]) -> list[OutMsg]:
        """Process one priority event; returns alert + state outputs."""
        out: list[OutMsg] = []
        self._refresh_dark(ev)
        tenant = self._tenant_of(ev.vin)
        for finding in evaluate_priority(ev, self.config.watchlist, self.config.approved, self.config.charger_cfg):
            alert = self.ledger.maybe_emit(
                ev.vin, tenant, finding, ev.ts_ms, ev.ingest_ts_ms, ev.lat, ev.lon, headers)
            if alert is None:
                self.metrics.suppressed.labels("priority", finding.rule).inc()
                continue
            out.append(OutMsg(C.ALERTS, ev.vin.encode(), alert.SerializeToString(), dict(headers)))
            self.metrics.alerts.labels("priority", finding.rule).inc()
            self.metrics.priority_latency.observe(max(self._clock() - ev.ts_ms, 0) / 1000.0)
        for vin, deadline in self._wheel.advance(self._watermark):
            out.extend(self._dark_alert(vin, deadline, headers))
        return out

    def handle_batch(self, msgs: list[tuple[InMsg, telemetry_pb2.TelemetryEvent]]) -> list[OutMsg]:
        t0 = time.perf_counter()
        out: list[OutMsg] = []
        for msg, ev in msgs:
            out.extend(self.handle(ev, msg.headers))
        log.info("priority_batch", inputs=len(msgs), alerts=len(out), watermark=self._watermark)
        self.metrics.batch_seconds.labels("priority").observe(time.perf_counter() - t0)
        return out

    # ------------------------------------------------------------------ dark
    def _refresh_dark(self, ev: telemetry_pb2.TelemetryEvent) -> None:
        if ev.ts_ms > self._watermark:
            self._watermark = ev.ts_ms
        self._last_seen[ev.vin] = ev.ts_ms
        self._wheel.schedule(ev.vin, ev.ts_ms + self.config.dark_timeout_ms)

    def _dark_alert(self, vin: str, deadline_ms: int, headers: dict[str, bytes]) -> list[OutMsg]:
        tenant = self._tenant_of(vin)
        finding = Finding(
            rule="GOING_DARK", summary=f"{vin} going dark: no data since deadline",
            details={"deadline_ms": str(deadline_ms)},
        )
        alert = self.ledger.maybe_emit(
            vin, tenant, finding, deadline_ms, self._clock(), 0.0, 0.0, headers,
            window_start_ms=deadline_ms)
        if alert is None:
            self.metrics.suppressed.labels("priority", "GOING_DARK").inc()
            return []
        self.metrics.alerts.labels("priority", "GOING_DARK").inc()
        return [OutMsg(C.ALERTS, vin.encode(), alert.SerializeToString(), dict(headers))]

    # ------------------------------------------------------------------ state
    def snapshot(self) -> dict:
        return {"watermark": self._watermark, "last_seen": dict(self._last_seen),
                "ledger": self.ledger.snapshot()}

    def restore(self, data: dict) -> None:
        self._watermark = data.get("watermark", -1)
        self._last_seen = dict(data.get("last_seen", {}))
        self.ledger.restore(data.get("ledger", {}))
        self._wheel = TimingWheel(tick_ms=1000)
        for vin, ts in self._last_seen.items():
            self._wheel.schedule(vin, ts + self.config.dark_timeout_ms)
        # Do NOT advance here: restored deadlines stay armed and fire on the next event's watermark.
