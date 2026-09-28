"""Online trip/stop and idle detection with hysteresis. O(1) per sample, O(1) state per vehicle.

Trip: starts after ``start_confirm`` consecutive moving samples (speed >= start_kmh, ignition on) and ends when the
vehicle has been below ``stop_kmh`` (or ignition off) for ``stop_hold_ms``. The thresholds differ (start > stop) and the
stop needs a hold time: that is the hysteresis that stops a red light from splitting a trip in two. The batch PELT
segmentation (algorithms.pelt) is the authoritative correction of this fast approximation.

Idle: ignition on and speed < ``idle_kmh``. A segment is reported once it ends (movement, ignition off, or a gap in the
data) if it lasted at least ``min_idle_ms``.
"""
from __future__ import annotations

from dataclasses import dataclass

from .geo import haversine_km

MAX_PLAUSIBLE_KMH = 250.0


@dataclass(frozen=True, slots=True)
class Sample:
    ts_ms: int
    speed_kmh: float
    ignition: bool
    lat: float
    lon: float
    odo_km: float = 0.0


@dataclass(frozen=True, slots=True)
class Trip:
    vin: str
    start_ts_ms: int
    end_ts_ms: int
    start_lat: float
    start_lon: float
    end_lat: float
    end_lon: float
    distance_km: float
    max_speed_kmh: float
    n_samples: int


@dataclass(frozen=True, slots=True)
class IdleSegment:
    vin: str
    start_ts_ms: int
    end_ts_ms: int
    lat: float
    lon: float

    @property
    def seconds(self) -> float:
        return (self.end_ts_ms - self.start_ts_ms) / 1000.0


@dataclass(slots=True)
class TripParams:
    start_kmh: float = 8.0
    stop_kmh: float = 3.0
    start_confirm: int = 2
    stop_hold_ms: int = 120_000


def _enc(x: Sample | None) -> list | None:
    return None if x is None else [x.ts_ms, x.speed_kmh, x.ignition, x.lat, x.lon, x.odo_km]


def _dec(x: list | None) -> Sample | None:
    return None if x is None else Sample(x[0], x[1], bool(x[2]), x[3], x[4], x[5])


class TripDetector:
    def __init__(self, vin: str, params: TripParams | None = None) -> None:
        self.vin = vin
        self.p = params or TripParams()
        self.in_trip = False
        self.last_ts = -1
        self._run: list[Sample] = []          # candidate moving run before a trip starts
        self._start: Sample | None = None
        self._last: Sample | None = None      # last moving sample of the trip
        self._stop_from: Sample | None = None
        self._dist = 0.0
        self._max_speed = 0.0
        self._n = 0
        self.out_of_order = 0

    @staticmethod
    def _leg(a: Sample, b: Sample) -> float:
        d = haversine_km(a.lat, a.lon, b.lat, b.lon)
        dt_h = (b.ts_ms - a.ts_ms) / 3_600_000
        if dt_h > 0 and d / dt_h > MAX_PLAUSIBLE_KMH:
            return 0.0  # GPS jump: excluded from distance
        return d

    def update(self, s: Sample) -> Trip | None:
        """Feed one sample. Returns a completed Trip when one ends (a start is visible via ``in_trip``)."""
        if s.ts_ms < self.last_ts:
            self.out_of_order += 1
            return None
        self.last_ts = s.ts_ms
        threshold = self.p.stop_kmh if self.in_trip else self.p.start_kmh
        moving = s.ignition and s.speed_kmh >= threshold
        if not self.in_trip:
            if moving:
                self._run.append(s)
                if len(self._run) >= self.p.start_confirm:
                    self._begin(self._run[0])
                    for x in self._run[1:]:
                        self._accumulate(x)
                    self._run = []
            else:
                self._run = []
            return None
        return self._continue(s, moving)

    def _begin(self, s: Sample) -> None:
        self.in_trip = True
        self._start = s
        self._last = s
        self._stop_from = None
        self._dist = 0.0
        self._max_speed = s.speed_kmh
        self._n = 1

    def _accumulate(self, s: Sample) -> None:
        assert self._last is not None
        self._dist += self._leg(self._last, s)
        self._last = s
        self._max_speed = max(self._max_speed, s.speed_kmh)
        self._n += 1

    def _continue(self, s: Sample, moving: bool) -> Trip | None:
        if moving:
            self._stop_from = None
            self._accumulate(s)
            return None
        if self._stop_from is None:
            self._stop_from = s
        if not s.ignition or s.ts_ms - self._stop_from.ts_ms >= self.p.stop_hold_ms:
            return self._finish()
        return None

    def _finish(self) -> Trip:
        assert self._start is not None and self._last is not None
        trip = Trip(
            vin=self.vin, start_ts_ms=self._start.ts_ms, end_ts_ms=max(self._last.ts_ms, self._start.ts_ms),
            start_lat=self._start.lat, start_lon=self._start.lon, end_lat=self._last.lat, end_lon=self._last.lon,
            distance_km=round(self._dist, 3), max_speed_kmh=self._max_speed, n_samples=self._n,
        )
        self.in_trip = False
        self._start = self._last = self._stop_from = None
        self._run = []
        return trip

    def snapshot(self) -> dict:
        return {"in": self.in_trip, "last_ts": self.last_ts, "run": [_enc(x) for x in self._run],
                "start": _enc(self._start), "last": _enc(self._last), "stop": _enc(self._stop_from),
                "dist": self._dist, "max": self._max_speed, "n": self._n}

    @classmethod
    def restore(cls, vin: str, d: dict, params: TripParams | None = None) -> TripDetector:
        t = cls(vin, params)
        t.in_trip, t.last_ts = d["in"], d["last_ts"]
        t._run = [_dec(x) for x in d["run"]]  # type: ignore[misc]
        t._start, t._last, t._stop_from = _dec(d["start"]), _dec(d["last"]), _dec(d["stop"])
        t._dist, t._max_speed, t._n = d["dist"], d["max"], d["n"]
        return t


class IdleDetector:
    def __init__(self, vin: str, idle_kmh: float = 1.0, min_idle_ms: int = 120_000, max_gap_ms: int = 600_000) -> None:
        self.vin = vin
        self.idle_kmh = idle_kmh
        self.min_idle_ms = min_idle_ms
        self.max_gap_ms = max_gap_ms
        self.start: Sample | None = None
        self.last: Sample | None = None

    @property
    def idle_ms(self) -> int:
        return 0 if self.start is None or self.last is None else self.last.ts_ms - self.start.ts_ms

    def update(self, s: Sample) -> IdleSegment | None:
        """Feed one sample; returns an IdleSegment when a qualifying idle period has just ended."""
        ended: IdleSegment | None = None
        if self.last is not None and s.ts_ms - self.last.ts_ms > self.max_gap_ms:
            ended = self._close()
        if s.ignition and s.speed_kmh < self.idle_kmh:
            if self.start is None:
                self.start = s
            self.last = s
            return ended
        seg = self._close()
        return ended or seg

    def _close(self) -> IdleSegment | None:
        seg = None
        if self.start is not None and self.last is not None and self.last.ts_ms - self.start.ts_ms >= self.min_idle_ms:
            seg = IdleSegment(self.vin, self.start.ts_ms, self.last.ts_ms, self.start.lat, self.start.lon)
        self.start = self.last = None
        return seg

    def snapshot(self) -> dict:
        return {"start": _enc(self.start), "last": _enc(self.last)}

    def restore_state(self, d: dict) -> None:
        self.start, self.last = _dec(d["start"]), _dec(d["last"])
