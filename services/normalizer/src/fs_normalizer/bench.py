"""Throughput benchmark: ``uv run --frozen python -m fs_normalizer.bench``.

Measures single-core records/s for (a) the reference ``Mapping.apply``, (b) the vectorised ``FastMapping.apply`` and
(c) the full normaliser path bytes -> validated -> Protobuf-serialised messages (incl. driver_ref encryption).
"""
from __future__ import annotations

import random
import sys
import time
from pathlib import Path

import orjson
import yaml
from fscore.mapping_engine import Mapping, to_proto
from fscore.mapping_fast import FastMapping
from fscore.vin import make_vin

from .core import NormalizerCore
from .drift import DriftMonitor
from .encryptor import AesGcmDevEncryptor, CachingEncryptor
from .metrics import NormalizerMetrics
from .models import InMsg
from .registry import MappingRegistry

ROOT = Path(__file__).resolve().parents[4]
MAPS = ROOT / "contracts" / "mappings"
NOW = int(time.time() * 1000)


def gen(oem: str, n: int, rng: random.Random, vins: list[str]) -> bytes:
    t0 = NOW - 60_000
    rows = []
    for i in range(n):
        vin, ts = rng.choice(vins), t0 + i
        lat, lon, spd, soc = 12.8 + rng.random() * 0.4, 77.4 + rng.random() * 0.4, round(rng.random() * 120, 1), round(rng.random() * 100, 1)
        iso = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts / 1000)) + f".{ts % 1000:03d}Z"
        dtc = rng.choice(["", "", "", "P0301", "P0301,P0171"])
        if oem == "OEM_A":
            rows.append({"vehicle": {"vin": vin, "sw": "4.2.1"}, "ts": iso, "seq": i, "telemetry": {"pos": {"lat": lat, "lng": lon}, "kph": spd, "odo_km": 1000.0 + i, "ign": True},
                         "battery": {"soc": soc, "soh": 96.5}, "diag": {"dtc": [c for c in dtc.split(",") if c]}, "event": rng.choice([None, None, "HARSH_BRAKE"]), "driver": f"drv_{vin[-4:]}"})
        elif oem == "OEM_B":
            rows.append({"v": vin, "t": ts, "la": lat, "lo": lon, "sp_mph": spd, "fl": soc, "od_mi": 500.0 + i, "codes": dtc, "ev": rng.choice(["", "", "HB"]), "ig": 1, "fw": "2.9.0", "dr": f"x{vin[-3:]}"})
        elif oem == "OEM_C":
            rows.append({"vin": vin, "ts": iso, "lat": lat, "lon": lon, "speed_kmh": spd, "soc_pct": soc, "odo_km": 100.0 + i, "dtc": [c for c in dtc.split(",") if c], "evt": rng.choice(["POSITION", "HARSH_BRAKE"]), "seq": i})
        else:
            rows.append([vin, ts // 1000, lat, lon, spd, round(rng.random(), 3), 0.965, 100.0 + i, dtc, rng.choice(["", "HB"])])
    if oem == "OEM_A":
        return orjson.dumps({"batch": "a-1", "records": rows})
    if oem == "OEM_B":
        return orjson.dumps({"bid": "b-1", "d": rows})
    if oem == "OEM_C":
        return orjson.dumps({"batch_id": "c-1", "events": rows})
    return orjson.dumps({"bid": "d-1", "schema": "nimbus/5.2", "cols": ["vin", "t", "la", "lo", "spd", "soc", "soh", "odo", "dtc", "e"], "rows": rows})


def rate(fn, records: int, reps: int = 5) -> float:
    best = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return records / best


def main(batch: int = 1000, n_batches: int = 8) -> dict[str, dict[str, float]]:
    rng = random.Random(7)
    vins = [make_vin("1HG", rng) for _ in range(2000)]
    results: dict[str, dict[str, float]] = {}
    reg = MappingRegistry()
    reg.load_dir(MAPS, "oem_a.yaml,oem_b.yaml,oem_c.yaml")
    reg.register(yaml.safe_load((MAPS / "oem_d_draft.yaml").read_text(encoding="utf-8")))
    core = NormalizerCore(reg, CachingEncryptor(AesGcmDevEncryptor(bytes(range(1, 33)))), DriftMonitor(), metrics=NormalizerMetrics())
    for oem in ("OEM_A", "OEM_B", "OEM_C", "OEM_D"):
        raws = [gen(oem, batch, rng, vins) for _ in range(n_batches)]
        parsed = [orjson.loads(r) for r in raws]
        m = reg.get(oem).mapping
        fm = FastMapping(m)
        total = batch * n_batches
        msgs = [InMsg("ingest.raw.v1", 0, i, f"{oem}:1".encode(), r, {"oem": oem.encode()}, NOW) for i, r in enumerate(raws)]
        ref = rate(lambda: [m.apply(p, NOW) for p in parsed], total)
        fast = rate(lambda: [fm.apply(p, NOW) for p in parsed], total)
        proto_ref = rate(lambda: [[to_proto(x, NOW).SerializeToString() for x in m.apply(p, NOW).ok] for p in parsed], total)
        full = rate(lambda: core.process(msgs), total)
        results[oem] = {"reference": ref, "fast": fast, "reference_plus_proto": proto_ref, "full_pipeline": full}
        print(f"{oem}: reference {ref:,.0f}/s | fast {fast:,.0f}/s ({fast / ref:.1f}x) | reference+proto {proto_ref:,.0f}/s | "
              f"full normaliser (json->proto bytes, encrypt) {full:,.0f}/s ({full / proto_ref:.1f}x vs reference+proto)")
    return results


if __name__ == "__main__":
    main(*(int(a) for a in sys.argv[1:3]))