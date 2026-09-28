"""Static domain constants: OEMs, vehicle models (mirror db/migrations/0005_reference.sql), paths, event codes."""
from __future__ import annotations

from pathlib import Path

OEM_CODES = ("OEM_A", "OEM_B", "OEM_C", "OEM_D")
# Synthetic World Manufacturer Identifiers (no real brands). Two per OEM so VINs look varied.
OEM_WMIS = {
    "OEM_A": ("1AU", "1AV"),
    "OEM_B": ("WKA", "WKB"),
    "OEM_C": ("MEC", "MER"),
    "OEM_D": ("5NB", "5NC"),
}
PT_ICE, PT_EV, PT_HYBRID = 0, 1, 2
PT_NAMES = ("ICE", "EV", "HYBRID")

# (oem_index, name, powertrain, battery_kwh, idle_fuel_l_per_h, idle_kw, tank_l)
# MUST mirror the vehicle_model reference rows in db/migrations/0005_reference.sql (a test enforces this).
MODELS = (
    (0, "Aurora Sprint", PT_ICE, None, 0.9, None, 45.0),
    (0, "Aurora Volt", PT_EV, 60.0, None, 1.2, 0.0),
    (1, "Kestrel Hauler", PT_ICE, None, 1.6, None, 90.0),
    (1, "Kestrel Hybrid", PT_HYBRID, 14.0, 0.6, 0.4, 50.0),
    (2, "Meridian City", PT_ICE, None, 0.7, None, 40.0),
    (2, "Meridian E", PT_EV, 75.0, None, 1.4, 0.0),
    (3, "Nimbus One", PT_EV, 82.0, None, 1.5, 0.0),
    (3, "Nimbus Lite", PT_EV, 48.0, None, 1.0, 0.0),
)
# Which OEMs can supply a powertrain (derived from MODELS; the reference data has hybrids only at OEM_B).
OEMS_FOR_POWERTRAIN = {
    PT_ICE: (0, 1, 2),
    PT_EV: (0, 2, 3),
    PT_HYBRID: (1,),
}

EVT_NAMES = (
    "POSITION", "HARSH_BRAKE", "HARSH_ACCEL", "IGNITION_ON", "IGNITION_OFF", "TOW", "CRASH",
    "CHARGING_START", "CHARGING_END", "DTC_SET", "OVERSPEED",
)
EVT = {n: i for i, n in enumerate(EVT_NAMES)}

# Vehicle states (Markov chain)
PARKED, DRIVING, IDLING, CHARGING = 0, 1, 2, 3
STATE_NAMES = ("parked", "driving", "idling", "charging")

# Bengaluru city centre; the synthetic grid is laid out around it.
CENTER_LAT, CENTER_LON = 12.9716, 77.5946
IST_OFFSET_S = 19_800

# DTC pools (all valid per fscore.dtc).
DTC_ICE = ("P0301", "P0302", "P0171", "P0420", "P0442", "P0300", "P0217", "P0524", "P0562", "P0700")
DTC_EV = ("P0A80", "P0AA6", "P0A0D", "P0AFA", "P1A00", "P0562", "U0100")
DTC_ALL = tuple(dict.fromkeys(DTC_ICE + DTC_EV))
DTC_INDEX = {c: i for i, c in enumerate(DTC_ALL)}

# USD-equivalent tariffs mirroring 0005_reference.sql (region -> energy -> [(hour_from, hour_to, price)])
TARIFFS = {
    "IN-KA": {
        "DIESEL": [(0, 24, 1.10)],
        "PETROL": [(0, 24, 1.25)],
        "ELECTRICITY": [(0, 6, 0.07), (6, 18, 0.12), (18, 22, 0.18), (22, 24, 0.09)],
    },
    "US-CA": {
        "DIESEL": [(0, 24, 1.60)],
        "PETROL": [(0, 24, 1.30)],
        "ELECTRICITY": [(0, 16, 0.22), (16, 21, 0.48), (21, 24, 0.26)],
    },
}


def repo_root() -> Path:
    """Walk up from this file to the directory holding contracts/demo_tenants.yaml."""
    for p in Path(__file__).resolve().parents:
        if (p / "contracts" / "demo_tenants.yaml").exists():
            return p
    return Path.cwd()


def demo_tenants_path() -> Path:
    return repo_root() / "contracts" / "demo_tenants.yaml"


def certs_dir() -> Path:
    return repo_root() / "deploy" / "certs"


def mappings_dir() -> Path:
    return repo_root() / "contracts" / "mappings"
