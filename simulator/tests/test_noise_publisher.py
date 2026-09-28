from __future__ import annotations

import asyncio
import json

import numpy as np
import pytest
from fscore.hashing import vin_shard

from fs_simulator import groundtruth as G
from fs_simulator.behaviour import SimConfig
from fs_simulator.constants import EVT, OEM_CODES
from fs_simulator.noise import NoiseInjector, concat_emissions
from fs_simulator.publisher import Connector, DriftControl, ListTransport, SimPublisher, is_event_lane
from helpers_sim import make_sim

CFG = SimConfig(emit_driving_s=1, emit_idle_s=2, emit_parked_s=3, emit_charging_s=3)
T_START = 1_790_000_000.0 - (1_790_000_000.0 % 86400) - 19800 + 8.05 * 3600        # inside the 3x burst window


def _pub(fleet, routes, **kw):
    kw.setdefault("cfg", CFG)
    return SimPublisher(fleet, routes, ListTransport(), seed=3, t0_s=T_START, **kw)


def _records(payload: bytes, oem: str):
    b = json.loads(payload)
    return b[{"OEM_A": "records", "OEM_B": "d", "OEM_C": "events", "OEM_D": "rows"}[oem]] if oem != "OEM_C" or "events" in b else b["events"]


def test_noise_rates_match_targets_and_conserve_records(fleet, routes):
    sim, _ = make_sim(fleet, routes, cfg=CFG)
    inj = NoiseInjector(np.random.default_rng(1))
    sent = recv = 0
    late = dup = 0
    for _ in range(200):
        em = sim.step(1.0)
        out, is_dup, is_late = inj.apply(em, sim.t)
        sent += len(em)
        recv += len(out)
        late += int(is_late.sum())
        dup += int(is_dup.sum())
    tail = inj.flush()
    recv += 0 if tail is None else len(tail)
    assert 0.012 < inj.stats.ooo_records / sent < 0.03 and 0.005 < inj.stats.dup_records / sent < 0.016
    assert recv == sent + inj.stats.dup_records          # nothing lost, duplicates added
    assert late > 0 and dup > 0


def test_late_records_arrive_after_newer_ones_for_same_vin(fleet, routes):
    sim, _ = make_sim(fleet, routes, cfg=CFG)
    inj = NoiseInjector(np.random.default_rng(2), ooo_p=0.3)
    last_seq = {}
    inversions = 0
    for _ in range(80):
        out, _, _ = inj.apply(sim.step(1.0), sim.t)
        for g, s in zip(out.gidx.tolist(), out.seq.tolist(), strict=True):
            inversions += s < last_seq.get(g, -1)
            last_seq[g] = max(s, last_seq.get(g, -1))
    assert inversions > 0


def test_concat_single_returns_same(fleet, routes):
    sim, _ = make_sim(fleet, routes, cfg=CFG)
    e = sim.step(1.0)
    assert concat_emissions([e]) is e and len(concat_emissions([e, e])) == 2 * len(e)


def test_connector_batches_at_100_or_250ms():
    c = Connector("OEM_A")
    recs = [{"i": i} for i in range(250)]
    c.add(recs, np.zeros(250, dtype=int), "telemetry", now=0.0, shape="OEM_A")
    msgs = c.flush_due(0.0, 1000)
    assert len(msgs) == 2                                            # two full batches of 100, 50 left waiting
    assert msgs[0][0] == "telemetry/OEM_A/0" and len(json.loads(msgs[0][1])["records"]) == 100
    assert c.flush_due(0.1, 1000) == []                              # < 250 ms: keep buffering
    tail = c.flush_due(0.26, 1000)
    assert len(tail) == 1 and len(json.loads(tail[0][1])["records"]) == 50
    c.add([{"x": 1}], np.array([7]), "event", now=1.0, shape="OEM_A")
    ev = c.flush_due(1.0, 1000)                                      # event lane is never delayed
    assert ev[0][0] == "event/OEM_A/7"


def test_topics_shard_by_vin_and_lanes(fleet, routes):
    pub = _pub(fleet, routes, noise=False, batch_retry_p=0.0)
    for i in range(30):
        pub.tick(1.0, now=float(i))
    msgs = pub._flush(100.0, force=True)
    assert pub.stats.events > 0
    seen_lanes = set()
    for topic, payload in msgs:
        lane, oem, shard = topic.split("/")
        assert oem in OEM_CODES and 0 <= int(shard) < 64
        seen_lanes.add(lane)
        b = json.loads(payload)
        if oem == "OEM_D":
            continue
        for r in _records(payload, oem):
            vin = r["vehicle"]["vin"] if oem == "OEM_A" else r.get("v") or r["vin"]
            assert vin_shard(vin) == int(shard)
    assert "telemetry" in seen_lanes


def test_event_lane_gets_critical_events_and_watchlist(fleet, routes):
    cfg = SimConfig(emit_driving_s=1, emit_idle_s=2, emit_parked_s=3, emit_charging_s=3, fault_scale=400.0, plan_days=0.05)
    pub = _pub(fleet, routes, cfg=cfg, noise=False, batch_retry_p=0.0)
    drift = pub.drift
    drift.force(True)
    topics = []
    for i in range(120):
        topics += [t for t, _ in pub.tick(1.0, now=float(i))]
    lanes = {t.split("/")[0] for t in topics}
    assert lanes == {"telemetry", "event"}
    em = pub.sim.step(1.0)
    ev = is_event_lane(em, fleet.watchlist)
    assert ev[fleet.watchlist[em.gidx]].all()
    crit = (em.evt == EVT["TOW"]) | (em.evt == EVT["CRASH"])
    assert ev[crit].all()


def test_oem_d_withheld_until_drift_then_columnar_and_recorded(fleet, routes):
    pub = _pub(fleet, routes, noise=False, batch_retry_p=0.0)
    before = [m for i in range(20) for m in pub.tick(1.0, now=float(i))] + pub._flush(50.0, force=True)
    assert not any("/OEM_D/" in t for t, _ in before) and pub.stats.withheld_oem_d > 0
    pub.drift.force(True)
    after = [m for i in range(20) for m in pub.tick(1.0, now=100.0 + i)] + pub._flush(200.0, force=True)
    d = [(t, p) for t, p in after if "/OEM_D/" in t]
    assert d and all("cols" in json.loads(p) and json.loads(p)["schema"] == "nimbus/5.2" for _, p in d)
    assert pub.stats.drift_started_ms is not None and pub.gt.count(G.SCHEMA_DRIFT) == 1


def test_oem_d_legacy_mode_uses_oem_c_shape_before_drift(fleet, routes):
    pub = _pub(fleet, routes, noise=False, batch_retry_p=0.0, d_mode="legacy")
    msgs = [m for i in range(20) for m in pub.tick(1.0, now=float(i))] + pub._flush(50.0, force=True)
    d = [json.loads(p) for t, p in msgs if "/OEM_D/" in t]
    assert d and all("events" in b and "batch_id" in b for b in d)


def test_batch_retries_resend_identical_payload_and_ledger_counts(fleet, routes):
    pub = _pub(fleet, routes, batch_retry_p=0.5)
    msgs = [m for i in range(15) for m in pub.tick(1.0, now=float(i))]
    assert pub.stats.batch_retries > 0 and len(msgs) > len({p for _, p in msgs})   # identical payload sent twice
    led = pub.ledger
    assert led.unique.sum() > 0 and led.dups.sum() > 0 and led.late.sum() >= 0
    assert (led.bulk + led.event).sum() == led.unique.sum() + led.dups.sum()
    seen = led.unique > 0
    assert (led.last_seq[seen] >= led.first_seq[seen]).all()


def test_drift_control_file_trigger(tmp_path):
    f = tmp_path / "ctl.json"
    d = DriftControl(f, check_every_s=0.0)
    assert not d.active(1.0)
    f.write_text('{"oem_d_drift": false}')
    assert not d.active(2.0)
    f.write_text('{"oem_d_drift": true}')
    assert d.active(3.0) and d.active(4.0)               # latches once triggered
    f.write_text("not json")
    assert DriftControl(f, 0.0).active(1.0) is False


def test_async_run_publishes_and_flushes(fleet, routes):
    pub = _pub(fleet, routes)
    stats = asyncio.run(pub.run(5.0, dt=1.0, realtime=False))
    assert stats.ticks == 5 and pub.transport.messages > 0 and len(pub.transport.sent) == pub.transport.messages
    assert pub.gt.count(G.BURST) >= 1


@pytest.mark.parametrize("n", [0, 1])
def test_connector_empty_and_tiny(n):
    c = Connector("OEM_B")
    if n:
        c.add([{"a": 1}], np.array([3]), "telemetry", 0.0, "OEM_B")
    assert len(c.flush_due(1.0, 5)) == n
