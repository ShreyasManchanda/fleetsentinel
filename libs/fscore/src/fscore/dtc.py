"""OBD-II Diagnostic Trouble Code parsing.

Public SAE J2012 structure only: 1 letter (system) + 4 hex digits. Descriptions are a small generic subset;
the copilot's knowledge base (embedded separately) covers more.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DTC_RE = re.compile(r"^[PBCU][0-9A-F]{4}$")

SYSTEMS = {"P": "powertrain", "B": "body", "C": "chassis", "U": "network"}

# 3rd character subsystem for P codes (generic table).
_P_SUBSYSTEM = {
    "0": "fuel_air_metering_aux_emission", "1": "fuel_air_metering", "2": "fuel_air_metering_injector",
    "3": "ignition_misfire", "4": "aux_emission_controls", "5": "speed_idle_control",
    "6": "computer_output_circuits", "7": "transmission", "8": "transmission", "9": "transmission",
    "A": "hybrid_ev_propulsion", "B": "hybrid_ev_propulsion", "C": "hybrid_ev_propulsion",
}

DESCRIPTIONS = {
    "P0300": "Random/multiple cylinder misfire detected",
    "P0301": "Cylinder 1 misfire detected",
    "P0302": "Cylinder 2 misfire detected",
    "P0303": "Cylinder 3 misfire detected",
    "P0304": "Cylinder 4 misfire detected",
    "P0171": "System too lean (bank 1)",
    "P0172": "System too rich (bank 1)",
    "P0217": "Engine coolant over-temperature condition",
    "P0420": "Catalyst system efficiency below threshold (bank 1)",
    "P0442": "Evaporative emission system leak detected (small)",
    "P0455": "Evaporative emission system leak detected (large)",
    "P0524": "Engine oil pressure too low",
    "P0562": "System voltage low",
    "P0601": "Internal control module memory checksum error",
    "P0700": "Transmission control system malfunction",
    "P0A0D": "Drive motor / high-voltage interlock circuit high",
    "P0A80": "Replace hybrid/EV battery pack",
    "P0AA6": "High-voltage isolation fault",
    "P0AFA": "Hybrid/EV battery system voltage low",
    "P1A00": "Battery thermal management performance",
    "B0001": "Driver frontal stage-1 deployment control",
    "C0035": "Left front wheel speed sensor circuit",
    "C0040": "Right front wheel speed sensor circuit",
    "U0100": "Lost communication with ECM/PCM",
    "U0121": "Lost communication with ABS control module",
}

# Codes that should raise a CRITICAL alert on the priority lane (safety- or damage-critical).
CRITICAL_DTCS = frozenset({"P0217", "P0524", "P0AA6", "P0A0D", "P0AFA", "B0001", "U0100", "P0300"})


@dataclass(frozen=True, slots=True)
class Dtc:
    code: str
    system: str          # powertrain | body | chassis | network
    generic: bool        # SAE-defined (True) vs manufacturer-specific (False)
    subsystem: str | None
    description: str | None
    critical: bool


def is_valid_dtc(code: str) -> bool:
    return isinstance(code, str) and DTC_RE.match(code) is not None


def parse_dtc(code: str) -> Dtc:
    """Parse and classify a DTC. Raises ValueError on a malformed code."""
    code = code.strip().upper()
    if not DTC_RE.match(code):
        raise ValueError(f"invalid DTC: {code!r}")
    letter, d2 = code[0], code[1]
    if letter == "P":
        generic = d2 in {"0", "2"} or (d2 == "3" and code[2] in "456789")
    else:  # B, C, U: 0 is generic, 1/2 manufacturer, 3 reserved (treated as manufacturer)
        generic = d2 == "0"
    subsystem = _P_SUBSYSTEM.get(code[2]) if letter == "P" else None
    return Dtc(
        code=code,
        system=SYSTEMS[letter],
        generic=generic,
        subsystem=subsystem,
        description=DESCRIPTIONS.get(code),
        critical=code in CRITICAL_DTCS,
    )


def parse_dtc_list(raw: str | list[str] | None) -> list[str]:
    """Normalize a raw DTC field (list, or comma/space/semicolon separated string) into valid, unique codes."""
    if raw is None:
        return []
    items = re.split(r"[,\s;]+", raw) if isinstance(raw, str) else list(raw)
    out: list[str] = []
    for item in items:
        c = str(item).strip().upper()
        if c and DTC_RE.match(c) and c not in out:
            out.append(c)
    return out
