"""VIN validation and generation (ISO 3779 / FMVSS 115 check digit).

Used by the simulator (to emit valid VINs), the normalizer/mapping engine (to reject bad ones) and the API.
"""
from __future__ import annotations

import random
import re

VIN_RE = re.compile(r"^[A-HJ-NPR-Z0-9]{17}$")  # 17 chars, no I, O, Q

_TRANSLIT = {
    **{str(d): d for d in range(10)},
    "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
    "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
    "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9,
}
_WEIGHTS = (8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2)
_VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"  # excludes I, O, Q


def check_digit(vin: str) -> str:
    """Return the expected check digit ('0'-'9' or 'X') for a 17-char VIN (position 9 is ignored)."""
    total = sum(_TRANSLIT[c] * w for c, w in zip(vin, _WEIGHTS, strict=True))
    rem = total % 11
    return "X" if rem == 10 else str(rem)


def is_valid_vin(vin: str) -> bool:
    if not isinstance(vin, str) or not VIN_RE.match(vin):
        return False
    return vin[8] == check_digit(vin)


def normalize_vin(vin: str) -> str:
    """Upper-case and strip whitespace. Does not validate."""
    return vin.strip().upper()


def make_vin(wmi: str, rng: random.Random) -> str:
    """Generate a valid VIN. `wmi` is the 3-char World Manufacturer Identifier; the rest is random."""
    if len(wmi) != 3 or not VIN_RE.match(wmi + "0" * 14):
        raise ValueError(f"invalid WMI: {wmi!r}")
    body = [rng.choice(_VIN_CHARS) for _ in range(13)]
    # VIN positions (1-indexed): 1-3 WMI, 4-8 VDS, 9 check digit, 10 model year, 11 plant, 12-17 serial.
    chars = list(wmi) + body[:5] + ["0"] + body[5:]
    chars[16] = rng.choice("0123456789")  # serial tail is numeric on North-American VINs; keeps them readable
    provisional = "".join(chars)
    chars[8] = check_digit(provisional)
    return "".join(chars)
