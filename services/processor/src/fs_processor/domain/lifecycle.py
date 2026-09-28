"""Alert lifecycle: deterministic ids, cooldowns, occurrence counting. Pure domain.

A repeat of an open (vehicle, rule) pair inside the cooldown increments ``occurrences``
instead of emitting a duplicate. Replays therefore produce identical ids and no duplicates.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from fleetsentinel.v1 import alert_pb2
from fscore import contracts as C

from .rules import RULE_TO_ENUM, RULE_TO_SEVERITY, Finding

DEFAULT_COOLDOWNS_MS = {
    "TOW": 3_600_000,
    "CRASH": 0,
    "CRITICAL_DTC": 3_600_000,
    "GEOFENCE_BREACH": 3_600_000,
    "LOW_SOC_STRANDING": 3_600_000,
    "GOING_DARK": 3_600_000,
    "EXCESSIVE_IDLING": 3_600_000,
    "HARSH_DRIVING": 3_600_000,
    "MAINTENANCE_RISK": 86_400_000,
    "UNAPPROVED_DEPOT_CLUSTER": 3_600_000,
    "ODOMETER_ROLLBACK": 86_400_000,
}


@dataclass(slots=True)
class OpenAlert:
    alert_id: str
    occurrences: int = 1
    last_emitted_ms: int = 0
    status: str = "open"


def trace_id_of(headers: dict[str, bytes]) -> str:
    raw = headers.get(C.H_TRACEPARENT, b"").decode(errors="replace")
    parts = raw.split("-")
    return parts[1] if len(parts) >= 3 else ""


class AlertLedger:
    """Tracks open (vin, rule) alerts. Snapshot/restore for checkpointing."""

    def __init__(
        self,
        cooldowns_ms: dict[str, int] | None = None,
        clock: Callable[[], int] | None = None,
    ) -> None:
        self.cooldowns = dict(DEFAULT_COOLDOWNS_MS)
        if cooldowns_ms:
            self.cooldowns.update(cooldowns_ms)
        self._clock = clock or (lambda: int(time.time() * 1000))
        self._open: dict[tuple[str, str], OpenAlert] = {}

    def __len__(self) -> int:
        return len(self._open)

    def open_count(self, vin: str) -> int:
        return sum(1 for (v, _r) in self._open if v == vin)

    def maybe_emit(
        self,
        vin: str,
        tenant_id: str,
        finding: Finding,
        event_ts_ms: int,
        ingest_ts_ms: int,
        lat: float,
        lon: float,
        headers: dict[str, bytes],
        window_start_ms: int | None = None,
    ) -> alert_pb2.Alert | None:
        """Emit an Alert, or None when suppressed by cooldown (occurrences incremented)."""
        window = event_ts_ms if window_start_ms is None else window_start_ms
        key = (vin, finding.rule)
        prev = self._open.get(key)
        now = self._clock()
        if prev is not None and now - prev.last_emitted_ms < self.cooldowns.get(finding.rule, 0):
            prev.occurrences += 1
            return None
        alert_id = C.alert_id(vin, finding.rule, window)
        alert = alert_pb2.Alert(
            alert_id=alert_id, tenant_id=tenant_id, vin=vin,
            rule=RULE_TO_ENUM[finding.rule], severity=RULE_TO_SEVERITY[finding.rule],
            window_start_ms=window, event_ts_ms=event_ts_ms, detected_ts_ms=now,
            lat=lat, lon=lon, summary=finding.summary, impact_usd=finding.impact_usd,
            ingest_ts_ms=ingest_ts_ms, trace_id=trace_id_of(headers),
        )
        for k, v in finding.details.items():
            alert.details[k] = v
        occurrences = prev.occurrences + 1 if prev is not None else 1
        self._open[key] = OpenAlert(alert_id, occurrences, now)
        alert.details["occurrences"] = str(occurrences)
        return alert

    def resolve(self, vin: str, rule: str) -> bool:
        return self._open.pop((vin, rule), None) is not None

    def snapshot(self) -> dict[str, dict]:
        return {f"{v}|{r}": {"id": a.alert_id, "n": a.occurrences, "ts": a.last_emitted_ms}
                for (v, r), a in self._open.items()}

    def restore(self, data: dict[str, dict]) -> None:
        self._open = {}
        for k, v in data.items():
            vin, _, rule = k.partition("|")
            self._open[(vin, rule)] = OpenAlert(v["id"], v["n"], v["ts"])
