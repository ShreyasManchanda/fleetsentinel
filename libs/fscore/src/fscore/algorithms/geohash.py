"""Geohash encode/decode/neighbours (base32). O(precision) per call.

Precision 5 is about 4.9 km cells, 7 about 153 m. ``neighbours`` returns the 8 surrounding cells (fewer at the poles);
longitude wraps across the antimeridian.
"""
from __future__ import annotations

_B32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DEC = {c: i for i, c in enumerate(_B32)}


def encode(lat: float, lon: float, precision: int = 7) -> str:
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise ValueError("lat/lon out of range")
    if not (1 <= precision <= 12):
        raise ValueError("precision 1..12")
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    out = []
    bits = 0
    ch = 0
    even = True
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch = (ch << 1) | 1
                lon_lo = mid
            else:
                ch <<= 1
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch = (ch << 1) | 1
                lat_lo = mid
            else:
                ch <<= 1
                lat_hi = mid
        even = not even
        bits += 1
        if bits == 5:
            out.append(_B32[ch])
            bits = 0
            ch = 0
    return "".join(out)


def decode_bbox(gh: str) -> tuple[float, float, float, float]:
    """Return (lat_min, lat_max, lon_min, lon_max)."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for c in gh:
        v = _DEC[c]
        for shift in range(4, -1, -1):
            bit = (v >> shift) & 1
            if even:
                mid = (lon_lo + lon_hi) / 2
                if bit:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if bit:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def decode(gh: str) -> tuple[float, float]:
    la0, la1, lo0, lo1 = decode_bbox(gh)
    return (la0 + la1) / 2, (lo0 + lo1) / 2


def neighbours(gh: str) -> list[str]:
    """The (up to) 8 adjacent cells of the same precision."""
    la0, la1, lo0, lo1 = decode_bbox(gh)
    dlat, dlon = la1 - la0, lo1 - lo0
    clat, clon = (la0 + la1) / 2, (lo0 + lo1) / 2
    out: list[str] = []
    for di in (-1, 0, 1):
        for dj in (-1, 0, 1):
            if di == 0 and dj == 0:
                continue
            lat = clat + di * dlat
            if not (-90.0 <= lat <= 90.0):
                continue
            lon = clon + dj * dlon
            if lon > 180.0:
                lon -= 360.0
            elif lon < -180.0:
                lon += 360.0
            n = encode(lat, lon, len(gh))
            if n != gh and n not in out:
                out.append(n)
    return out
