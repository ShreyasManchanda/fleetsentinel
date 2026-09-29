"""Differential tests: FastMapping.apply must equal Mapping.apply on every input (ok dicts AND failures)."""
import copy
import json
import math
import random
from pathlib import Path

import pytest
import yaml
from fscore.mapping_engine import Mapping
from fscore.mapping_fast import FastMapping
from fscore.vin import make_vin
from hypothesis import given, settings
from hypothesis import strategies as st

ROOT = Path(__file__).resolve().parents[3]
MAPS = ROOT / "contracts" / "mappings"
NOW = 1_790_590_600_000
OEMS = {"OEM_A": "oem_a", "OEM_B": "oem_b", "OEM_C": "oem_c", "OEM_D": "oem_d"}


def load(oem: str) -> Mapping:
    name = "oem_d_draft.yaml" if oem == "OEM_D" else f"{OEMS[oem]}.yaml"
    return Mapping(yaml.safe_load((MAPS / name).read_text(encoding="utf-8")))


def sample(oem: str) -> dict:
    return json.loads((MAPS / "samples" / f"{OEMS[oem]}.json").read_text(encoding="utf-8"))


def _records_key(oem):
    return {"OEM_A": "records", "OEM_B": "d", "OEM_C": "events", "OEM_D": "rows"}[oem]


def gen_batch(oem: str, rng: random.Random, n: int) -> dict:
    tpl = sample(oem)
    key = _records_key(oem)
    vins = [make_vin("1HG", rng) for _ in range(max(1, n // 5))]
    out = copy.deepcopy(tpl)
    recs = []
    for i in range(n):
        base = copy.deepcopy(tpl[key][i % len(tpl[key])])
        vin = rng.choice(vins)
        ts = 1_790_590_000_000 + (i % 500) * 997
        lat, lon = 12.8 + rng.random() * 0.4, 77.4 + rng.random() * 0.4
        spd = round(rng.random() * 120, 1)
        soc = round(rng.random() * 100, 1)
        if oem == "OEM_A":
            base["vehicle"]["vin"] = vin
            base["ts"] = f"2026-09-28T10:{(i // 60) % 60:02d}:{i % 60:02d}.{i % 1000:03d}Z"
            base["telemetry"]["pos"] = {"lat": lat, "lng": lon}
            base["telemetry"]["kph"] = spd
            base["battery"]["soc"] = soc
            base["seq"] = i
            base["diag"]["dtc"] = rng.choice([[], ["P0301"], ["P0300", "P0217"]])
        elif oem == "OEM_B":
            base.update(v=vin, t=ts, la=lat, lo=lon, sp_mph=spd, fl=soc, od_mi=1000 + i * 0.1,
                        codes=rng.choice(["", "P0301", "P0301,P0171"]), ev=rng.choice(["", "HB", "TW", "OS"]),
                        # NB: no unmapped codes here — clean batches must be fully clean since B-021
                        # (unknown evt fails to DLQ); unknown-code equivalence is covered by corrupt() paths.
                        ig=rng.choice([0, 1]))
        elif oem == "OEM_C":
            base.update(vin=vin, ts=f"2026-09-28T10:00:{i % 60:02d}.{i % 1000:03d}Z", lat=lat, lon=lon,
                        speed_kmh=spd, soc_pct=soc, seq=i)
            if rng.random() < 0.3:
                base.pop("ignition", None)
        else:
            base = [vin, 1_790_590_000 + i, lat, lon, spd, round(rng.random(), 3), 0.9, 100.0 + i,
                    rng.choice(["", "P0301"]), rng.choice(["", "HB", "TOW"])]
        recs.append(base)
    out[key] = recs
    return out


def corrupt(oem: str, payload: dict, rng: random.Random, p: float) -> dict:
    key = _records_key(oem)
    for i, r in enumerate(payload[key]):
        if rng.random() >= p:
            continue
        kind = rng.choice(["drop", "none", "garbage", "range", "vin", "future", "old", "type", "shape"])
        if oem == "OEM_D":
            j = rng.randrange(len(r))
            if kind == "shape":
                r.pop()
            elif kind == "vin":
                r[0] = "BADVIN"
            elif kind == "future":
                r[1] = 4_000_000_000
            elif kind == "old":
                r[1] = 1000
            elif kind == "range":
                r[2] = 123.0
            elif kind == "garbage":
                r[j] = "zz"
            elif kind == "none":
                r[j] = None
            elif kind == "type":
                r[j] = [1, 2]
            else:
                payload[key][i] = 7
            continue
        flat = {"OEM_A": "vehicle", "OEM_B": "v", "OEM_C": "vin"}[oem]
        if kind == "shape":
            payload[key][i] = 5 if rng.random() < 0.5 else "junk"
        elif kind == "vin":
            if oem == "OEM_A":
                r["vehicle"]["vin"] = "1HGCM82633A004353"
            else:
                r[flat] = "1HGCM82633A004353"
        elif kind == "drop":
            victim = rng.choice([k for k in r if k != flat] or list(r))
            r.pop(victim, None)
        elif kind == "none":
            victim = rng.choice(list(r))
            r[victim] = None
        elif kind == "garbage":
            victim = rng.choice([k for k in r if k not in ("vehicle", "telemetry", "battery", "diag")] or list(r))
            r[victim] = "garbage"
        elif kind == "range":
            tgt = {"OEM_A": None, "OEM_B": "la", "OEM_C": "lat"}[oem]
            if tgt:
                r[tgt] = 500.0
            else:
                r["telemetry"]["pos"]["lat"] = -400.0
        elif kind == "future":
            tsk = {"OEM_A": "ts", "OEM_B": "t", "OEM_C": "ts"}[oem]
            r[tsk] = "2999-01-01T00:00:00Z" if oem != "OEM_B" else 99_999_999_999_999
        elif kind == "old":
            tsk = {"OEM_A": "ts", "OEM_B": "t", "OEM_C": "ts"}[oem]
            r[tsk] = "2001-01-01T00:00:00Z" if oem != "OEM_B" else 5
        elif kind == "type":
            victim = rng.choice(list(r))
            r[victim] = {"x": [1]}
    return payload


def same(ref, fast):
    assert fast.oem == ref.oem and fast.mapping_version == ref.mapping_version and fast.batch_id == ref.batch_id
    assert fast.ok == ref.ok
    assert [(f.reason, f.detail, f.raw) for f in fast.failed] == [(f.reason, f.detail, f.raw) for f in ref.failed]


@pytest.mark.parametrize("oem", list(OEMS))
def test_samples_identical(oem):
    m = load(oem)
    same(m.apply(sample(oem), NOW), FastMapping(m).apply(sample(oem), NOW))


@pytest.mark.parametrize("oem", list(OEMS))
@pytest.mark.parametrize("p", [0.0, 0.05, 0.4])
def test_random_batches_identical(oem, p):
    m = load(oem)
    fm = FastMapping(m)
    rng = random.Random(f"{oem}{p}")
    for _ in range(12):
        payload = corrupt(oem, gen_batch(oem, rng, rng.choice([1, 7, 60, 300])), rng, p)
        same(m.apply(payload, NOW), fm.apply(payload, NOW))          # dict input
        raw = json.dumps(payload).encode()
        same(m.apply(raw, NOW), fm.apply(raw, NOW))                   # bytes input


@pytest.mark.parametrize("oem", list(OEMS))
def test_clean_batches_actually_take_the_fast_path(oem):
    m = load(oem)
    fm = FastMapping(m)
    rng = random.Random(9)
    res = fm.apply(gen_batch(oem, rng, 500), NOW)
    assert len(res.ok) == 500 and not res.failed


@pytest.mark.parametrize("payload", [b"not json", b"{}", b'{"d": 5}', b'{"d": []}', b"[]", '{"d":[{"v":1}]}'])
def test_envelope_problems_identical(payload):
    m = load("OEM_B")
    same(m.apply(payload, NOW), FastMapping(m).apply(payload, NOW))


def test_columnar_envelope_problems_identical():
    m = load("OEM_D")
    for payload in ({"cols": 3, "rows": []}, {"cols": ["a"], "rows": [[1], [1, 2], "x"]}, {"rows": [[1]]}):
        same(m.apply(payload, NOW), FastMapping(m).apply(payload, NOW))


def test_nan_and_inf_and_bool_types_route_to_reference():
    m = load("OEM_C")
    fm = FastMapping(m)
    base = sample("OEM_C")["events"][0]
    recs = []
    for k, v in (("lat", float("nan")), ("lon", float("inf")), ("speed_kmh", True), ("soc_pct", float("nan")),
                 ("ts", 12.5), ("odo_km", "abc")):
        r = dict(base)
        r[k] = v
        recs.append(r)
    payload = {"batch_id": "x", "events": recs + [dict(base) for _ in range(30)]}
    a, b = m.apply(payload, NOW), fm.apply(payload, NOW)
    assert [(f.reason, f.detail) for f in a.failed] == [(f.reason, f.detail) for f in b.failed]
    assert len(a.ok) == len(b.ok) and all(not math.isnan(x["lat"]) for x in b.ok)


def test_mostly_bad_batch_falls_back_to_reference_and_matches():
    m = load("OEM_B")
    rng = random.Random(4)
    payload = corrupt("OEM_B", gen_batch("OEM_B", rng, 100), rng, 1.0)
    same(m.apply(payload, NOW), FastMapping(m).apply(payload, NOW))


def test_memo_does_not_alias_lists_or_confuse_types():
    m = load("OEM_B")
    fm = FastMapping(m)
    payload = gen_batch("OEM_B", random.Random(1), 20)
    for r in payload["d"]:
        r["codes"] = "P0301,P0171"
    res = fm.apply(payload, NOW)
    res.ok[0]["dtc"].append("X")
    assert res.ok[1]["dtc"] == ["P0301", "P0171"]
    payload2 = {"bid": "b", "d": [dict(payload["d"][0], ig=1), dict(payload["d"][1], ig=True), dict(payload["d"][2], ig="1")]}
    same(m.apply(payload2, NOW), fm.apply(payload2, NOW))


@settings(deadline=None, max_examples=60)
@given(st.integers(0, 10_000), st.sampled_from(list(OEMS)), st.floats(0, 0.6), st.integers(1, 80))
def test_property_fast_equals_reference(seed, oem, p, n):
    rng = random.Random(seed)
    payload = corrupt(oem, gen_batch(oem, rng, n), rng, p)
    m = load(oem)
    same(m.apply(payload, NOW), FastMapping(m).apply(payload, NOW))