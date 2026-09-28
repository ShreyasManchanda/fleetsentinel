"""OEM payload encoders producing EXACTLY the shapes in contracts/mappings/samples/*.json.

OEM_A nested ISO ts | OEM_B flat abbreviated epoch-ms, mph/miles, CSV DTC | OEM_C brief shape | OEM_D columnar, epoch seconds,
fractional SoC. Each encoder maps an Emission (numpy struct-of-arrays) to a list of wire records; wrap_batch builds the
envelope. Every output round-trips through fscore.mapping_engine with the contract mappings (see tests).
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import orjson

from .constants import DTC_ALL, EVT_NAMES
from .fleet import Fleet

KM_PER_MILE = 1.609344
D_COLS = ["vin", "t", "la", "lo", "spd", "soc", "soh", "odo", "dtc", "e"]
D_SCHEMA = "nimbus/5.2"

_EVT_A = {"HARSH_BRAKE": "HARSH_BRAKE", "HARSH_ACCEL": "HARSH_ACCEL", "IGNITION_ON": "IGN_ON", "IGNITION_OFF": "IGN_OFF",
          "TOW": "TOWED", "CRASH": "CRASH_DETECTED", "CHARGING_START": "CHG_START", "CHARGING_END": "CHG_END",
          "DTC_SET": "DTC", "OVERSPEED": "OVERSPEED"}
_EVT_B = {"HARSH_BRAKE": "HB", "HARSH_ACCEL": "HA", "IGNITION_ON": "ON", "IGNITION_OFF": "OFF", "TOW": "TW", "CRASH": "CR",
          "OVERSPEED": "OS", "DTC_SET": "DT"}
_EVT_C = {"HARSH_BRAKE": "HARSH_BRAKE", "HARSH_ACCEL": "HARSH_ACCEL", "TOW": "TOW", "CRASH": "CRASH", "OVERSPEED": "OVERSPEED",
          "DTC_SET": "DTC_SET"}
_EVT_D = {"HARSH_BRAKE": "HB", "HARSH_ACCEL": "HA", "TOW": "TOW", "CRASH": "CRASH", "CHARGING_START": "CHG", "CHARGING_END": "CHGE"}


def _evt_table(m: dict[str, str], default):
    return [m.get(n, default) if n != "POSITION" else default for n in EVT_NAMES]


EVT_A, EVT_B = _evt_table(_EVT_A, None), _evt_table(_EVT_B, "")
EVT_C, EVT_D = _evt_table(_EVT_C, None), _evt_table(_EVT_D, "")
DTC_LIST = [c for c in DTC_ALL]


@dataclass
class EncodeContext:
    """Per-fleet static lookups (python lists, indexed by GLOBAL vehicle index)."""
    vin: list[str]
    sw: list[str]
    drv: list[str]

    @classmethod
    def from_fleet(cls, fleet: Fleet) -> EncodeContext:
        return cls(fleet.vin.tolist(), fleet.sw_versions().tolist(), fleet.driver_ref(fleet.driver_idx))


def iso_ms(ts_ms: np.ndarray) -> list[str]:
    """Vectorised epoch-ms -> 'YYYY-MM-DDTHH:MM:SS.mmmZ'."""
    return [s + "Z" for s in np.asarray(ts_ms, dtype="int64").astype("datetime64[ms]").astype(str).tolist()]


def _r(x: float, nd: int) -> float:
    return round(x, nd)


def encode_a(em, ctx: EncodeContext) -> list[dict]:
    g, ts = em.gidx.tolist(), iso_ms(em.ts_ms)
    lat, lon, spd, odo = em.lat.tolist(), em.lon.tolist(), em.speed.tolist(), em.odo.tolist()
    soc, soh, ign, seq = em.soc.tolist(), em.soh.tolist(), em.ign.tolist(), em.seq.tolist()
    evt, dtc = em.evt.tolist(), em.dtc.tolist()
    out = []
    for i in range(len(g)):
        gi = g[i]
        rec: dict = {"vehicle": {"vin": ctx.vin[gi], "sw": ctx.sw[gi]}, "ts": ts[i], "seq": seq[i],
                     "telemetry": {"pos": {"lat": _r(lat[i], 6), "lng": _r(lon[i], 6)}, "kph": _r(spd[i], 1),
                                   "odo_km": _r(odo[i], 1), "ign": ign[i]}}
        if not math.isnan(soc[i]):
            rec["battery"] = {"soc": _r(soc[i], 1), "soh": _r(soh[i], 1) if not math.isnan(soh[i]) else 100.0}
        rec["diag"] = {"dtc": [DTC_LIST[dtc[i]]] if dtc[i] >= 0 else []}
        rec["event"] = EVT_A[evt[i]]
        rec["driver"] = ctx.drv[gi]
        out.append(rec)
    return out


def encode_b(em, ctx: EncodeContext) -> list[dict]:
    g = em.gidx.tolist()
    lat, lon, spd, odo = em.lat.tolist(), em.lon.tolist(), em.speed.tolist(), em.odo.tolist()
    fuel, ign, ts, evt, dtc = em.fuel.tolist(), em.ign.tolist(), em.ts_ms.tolist(), em.evt.tolist(), em.dtc.tolist()
    out = []
    for i in range(len(g)):
        gi = g[i]
        rec = {"v": ctx.vin[gi], "t": ts[i], "la": _r(lat[i], 6), "lo": _r(lon[i], 6), "sp_mph": _r(spd[i] / KM_PER_MILE, 1)}
        if not math.isnan(fuel[i]):
            rec["fl"] = _r(fuel[i], 1)
        rec.update({"od_mi": _r(odo[i] / KM_PER_MILE, 1), "codes": DTC_LIST[dtc[i]] if dtc[i] >= 0 else "",
                    "ev": EVT_B[evt[i]], "ig": 1 if ign[i] else 0, "fw": ctx.sw[gi], "dr": ctx.drv[gi]})
        out.append(rec)
    return out


def encode_c(em, ctx: EncodeContext) -> list[dict]:
    g, ts = em.gidx.tolist(), iso_ms(em.ts_ms)
    lat, lon, spd, odo = em.lat.tolist(), em.lon.tolist(), em.speed.tolist(), em.odo.tolist()
    soc, ign, seq, evt, dtc = em.soc.tolist(), em.ign.tolist(), em.seq.tolist(), em.evt.tolist(), em.dtc.tolist()
    out = []
    for i in range(len(g)):
        rec = {"vin": ctx.vin[g[i]], "ts": ts[i], "lat": _r(lat[i], 6), "lon": _r(lon[i], 6), "speed_kmh": _r(spd[i], 1)}
        if not math.isnan(soc[i]):
            rec["soc_pct"] = _r(soc[i], 1)
        rec.update({"odo_km": _r(odo[i], 1), "dtc": [DTC_LIST[dtc[i]]] if dtc[i] >= 0 else [], "evt": EVT_C[evt[i]],
                    "seq": seq[i], "ignition": ign[i]})
        out.append(rec)
    return out


def encode_d_rows(em, ctx: EncodeContext) -> list[list]:
    g = em.gidx.tolist()
    ts = (em.ts_ms // 1000).tolist()
    lat, lon, spd, odo = em.lat.tolist(), em.lon.tolist(), em.speed.tolist(), em.odo.tolist()
    soc, soh, evt, dtc = em.soc.tolist(), em.soh.tolist(), em.evt.tolist(), em.dtc.tolist()
    return [[ctx.vin[g[i]], ts[i], _r(lat[i], 6), _r(lon[i], 6), _r(spd[i], 1),
             None if math.isnan(soc[i]) else _r(soc[i] / 100.0, 3), None if math.isnan(soh[i]) else _r(soh[i] / 100.0, 3),
             _r(odo[i], 1), DTC_LIST[dtc[i]] if dtc[i] >= 0 else "", EVT_D[evt[i]]] for i in range(len(g))]


ENCODERS = {"OEM_A": encode_a, "OEM_B": encode_b, "OEM_C": encode_c}
BATCH_PREFIX = {"OEM_A": "a", "OEM_B": "b", "OEM_C": "c", "OEM_D": "d"}


def wrap_batch(oem: str, records: list, batch_no: int, sent_ms: int, columnar_d: bool = True) -> bytes:
    """Envelope + serialise one batch (orjson bytes). OEM_D uses the columnar format (records are row lists)."""
    bid = f"{BATCH_PREFIX[oem]}-{batch_no:06d}"
    if oem == "OEM_A":
        obj = {"batch": bid, "sent": iso_ms(np.array([sent_ms]))[0], "records": records}
    elif oem == "OEM_B":
        obj = {"bid": bid, "d": records}
    elif oem == "OEM_C":
        obj = {"batch_id": bid, "events": records}
    else:
        obj = {"bid": bid, "schema": D_SCHEMA, "cols": D_COLS, "rows": records}
    return orjson.dumps(obj)


def encode(oem: str, em, ctx: EncodeContext, d_legacy: bool = False) -> tuple[str, list]:
    """Encode an Emission for an OEM. Returns (wire_oem_shape, records). OEM_D before drift may use OEM_C shape."""
    if oem == "OEM_D":
        return ("OEM_C", encode_c(em, ctx)) if d_legacy else ("OEM_D", encode_d_rows(em, ctx))
    return oem, ENCODERS[oem](em, ctx)
