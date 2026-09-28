"""OEM connector publisher core. Broker-free and testable: batching, sharding, lane routing, noise and the OEM_D
schema-drift trigger live here; the aiomqtt transport is in adapters/mqtt.py.

Lanes: bulk telemetry -> telemetry/{oem}/{shard}; critical events (TOW, CRASH, critical DTC) and lender-watchlist telemetry
-> event/{oem}/{shard}. A record goes to exactly one lane (critical/watchlist records are NOT duplicated on the bulk lane).
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np
import structlog
from fscore.contracts import MQTT_EVENT_TOPIC_FMT, MQTT_TELEMETRY_TOPIC_FMT
from fscore.dtc import CRITICAL_DTCS

from . import groundtruth as G
from .behaviour import Emission, FleetSim, SimConfig
from .constants import DTC_INDEX, EVT, OEM_CODES
from .encoders import EncodeContext, encode, wrap_batch
from .fleet import Fleet
from .groundtruth import GroundTruth, SeqLedger
from .noise import NoiseInjector
from .routes import RouteLibrary

log = structlog.get_logger("fs_simulator.publisher")
CRIT_IDX = np.array(sorted(DTC_INDEX[c] for c in CRITICAL_DTCS if c in DTC_INDEX))


class Transport(Protocol):
    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None: ...


class NullTransport:
    def __init__(self) -> None:
        self.messages = self.bytes = 0

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        self.messages += 1
        self.bytes += len(payload)


class ListTransport(NullTransport):
    def __init__(self) -> None:
        super().__init__()
        self.sent: list[tuple[str, bytes]] = []

    async def publish(self, topic: str, payload: bytes, qos: int = 1) -> None:
        await super().publish(topic, payload, qos)
        self.sent.append((topic, payload))


class DriftControl:
    """OEM_D schema-drift trigger: a control file {"oem_d_drift": true} or an in-process flag."""

    def __init__(self, path: Path | None = None, check_every_s: float = 1.0):
        self.path, self.check_every_s = path, check_every_s
        self._flag = False
        self._last = -1e9

    def force(self, on: bool = True) -> None:
        self._flag = on

    def active(self, now_mono: float | None = None) -> bool:
        if self._flag:
            return True
        now = time.monotonic() if now_mono is None else now_mono
        if self.path is not None and now - self._last >= self.check_every_s:
            self._last = now
            try:
                if json.loads(Path(self.path).read_text(encoding="utf-8")).get("oem_d_drift"):
                    self._flag = True
            except (OSError, ValueError):
                pass
        return self._flag


def is_event_lane(em: Emission, watchlist: np.ndarray) -> np.ndarray:
    crit = (em.evt == EVT["TOW"]) | (em.evt == EVT["CRASH"]) | ((em.evt == EVT["DTC_SET"]) & np.isin(em.dtc, CRIT_IDX))
    return crit | watchlist[em.gidx]


@dataclass
class Connector:
    """One OEM cloud connector: per-(lane, shard, shape) buffers flushed at batch_size records or flush_s seconds."""
    oem: str
    batch_size: int = 100
    flush_s: float = 0.25
    batch_no: int = 0
    buf: dict = field(default_factory=lambda: defaultdict(list))
    born: dict = field(default_factory=dict)
    messages: int = 0
    records: int = 0

    def add(self, records: list, shards: np.ndarray, lane: str, now: float, shape: str) -> None:
        order = np.argsort(shards, kind="stable")
        bounds = np.flatnonzero(np.diff(shards[order])) + 1
        for grp in np.split(order, bounds):
            if not len(grp):
                continue
            key = (lane, int(shards[grp[0]]), shape)
            self.buf[key].extend(records[i] for i in grp.tolist())
            self.born.setdefault(key, now)

    def _emit(self, key: tuple, recs: list, sent_ms: int) -> tuple[str, bytes]:
        self.batch_no += 1
        fmt = MQTT_EVENT_TOPIC_FMT if key[0] == "event" else MQTT_TELEMETRY_TOPIC_FMT
        self.messages += 1
        self.records += len(recs)
        return fmt.format(oem=self.oem, shard=key[1]), wrap_batch(key[2], recs, self.batch_no, sent_ms)

    def flush_due(self, now: float, sent_ms: int, force: bool = False) -> list[tuple[str, bytes]]:
        out = []
        for key in list(self.buf):
            recs = self.buf[key]
            while len(recs) >= self.batch_size:
                out.append(self._emit(key, recs[: self.batch_size], sent_ms))
                del recs[: self.batch_size]
            if recs and (force or now - self.born.get(key, now) >= self.flush_s or key[0] == "event"):
                out.append(self._emit(key, recs[:], sent_ms))
                recs.clear()
            if not recs:
                del self.buf[key]
                self.born.pop(key, None)
            else:
                self.born.setdefault(key, now)
        return out


@dataclass
class PubStats:
    ticks: int = 0
    events: int = 0
    messages: int = 0
    withheld_oem_d: int = 0
    drift_started_ms: int | None = None
    batch_retries: int = 0


class SimPublisher:
    """Drives a FleetSim, injects transport noise, encodes per OEM and hands batches to a Transport."""

    def __init__(self, fleet: Fleet, routes: RouteLibrary, transport: Transport, seed: int = 42, t0_s: float | None = None,
                 cfg: SimConfig | None = None, drift: DriftControl | None = None, d_mode: str = "withhold",
                 batch_size: int = 100, flush_s: float = 0.25, batch_retry_p: float = 0.01, noise: bool = True,
                 subset: np.ndarray | None = None):
        self.fleet, self.transport, self.d_mode = fleet, transport, d_mode
        self.gt = GroundTruth(fleet.vin)
        self.ledger = SeqLedger(fleet.vin)
        gidx = np.arange(fleet.n) if subset is None else np.asarray(subset)
        self.t0 = time.time() if t0_s is None else t0_s
        self.sim = FleetSim(fleet, routes, gidx, seed, self.t0, cfg, self.gt)
        self.rng = np.random.default_rng([seed, 9])
        self.noise = NoiseInjector(self.rng) if noise else None
        self.ctx = EncodeContext.from_fleet(fleet)
        self.conn = {o: Connector(o, batch_size, flush_s) for o in OEM_CODES}
        self.drift = drift or DriftControl()
        self.retry_p = batch_retry_p
        self.stats = PubStats()
        self._burst_day = -1

    def _ingest(self, em: Emission, is_dup: np.ndarray, is_late: np.ndarray, now: float) -> None:
        if not len(em):
            return
        ev = is_event_lane(em, self.fleet.watchlist)
        self.ledger.record(em.gidx, em.seq, is_dup, is_late, ev)
        oem = self.fleet.oem_idx[em.gidx]
        drifted = self.drift.active()
        if drifted and self.stats.drift_started_ms is None:
            self.stats.drift_started_ms = int(self.sim.t * 1000)
            self.gt.add_global(G.SCHEMA_DRIFT, self.stats.drift_started_ms, self.stats.drift_started_ms,
                               note="OEM_D switched to columnar nimbus/5.2")
        for oi, code in enumerate(OEM_CODES):
            m = oem == oi
            if not m.any():
                continue
            if code == "OEM_D" and not drifted and self.d_mode == "withhold":
                self.stats.withheld_oem_d += int(m.sum())
                continue
            legacy = code == "OEM_D" and not drifted
            for lane, lm in (("telemetry", m & ~ev), ("event", m & ev)):
                if not lm.any():
                    continue
                sub = em.select(lm)
                shape, recs = encode(code, sub, self.ctx, d_legacy=legacy)
                self.conn[code].add(recs, self.fleet.shard[sub.gidx], lane, now, shape)

    def _flush(self, now: float, force: bool = False) -> list[tuple[str, bytes]]:
        sent_ms = int(self.sim.t * 1000)
        out: list[tuple[str, bytes]] = []
        for c in self.conn.values():
            out.extend(c.flush_due(now, sent_ms, force))
        return out

    def tick(self, dt: float, now: float | None = None) -> list[tuple[str, bytes]]:
        """Advance the simulation one step and return the (topic, payload) messages due (no I/O)."""
        now = time.monotonic() if now is None else now
        em = self.sim.step(dt)
        if self.noise is not None:
            em, is_dup, is_late = self.noise.apply(em, self.sim.t)
        else:
            is_dup = is_late = np.zeros(len(em), dtype=bool)
        self._ingest(em, is_dup, is_late, now)
        msgs = self._flush(now)
        if msgs and self.retry_p > 0:
            retry = [m for m in msgs if self.rng.random() < self.retry_p]
            self.stats.batch_retries += len(retry)
            if self.noise is not None:
                self.noise.stats.batch_retries += len(retry)
                self.noise.stats.batch_retry_records += 0
            msgs = msgs + retry
        self.stats.ticks += 1
        self.stats.events += len(em)
        self.stats.messages += len(msgs)
        self._record_burst()
        return msgs

    def _record_burst(self) -> None:
        cfg, day = self.sim.cfg, self.sim.day
        if cfg.faults and day != self._burst_day:
            self._burst_day = day
            start = day * 86400.0 - 19800 + cfg.burst_local_hour * 3600.0
            self.gt.add_global(G.BURST, int(start * 1000), int((start + cfg.burst_len_s) * 1000), value=cfg.burst_factor,
                               note="3x emission rate at shift start")

    async def run(self, duration_s: float, dt: float = 1.0, realtime: bool = True) -> PubStats:
        t_wall0 = time.monotonic()
        for i in range(int(duration_s / dt)):
            msgs = self.tick(dt)
            if msgs:
                await asyncio.gather(*(self.transport.publish(t, p, 1) for t, p in msgs))
            if realtime:
                delay = t_wall0 + (i + 1) * dt - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
        for topic, payload in self._flush(time.monotonic(), force=True):
            await self.transport.publish(topic, payload, 1)
        log.info("publisher_done", **self.stats.__dict__)
        return self.stats
