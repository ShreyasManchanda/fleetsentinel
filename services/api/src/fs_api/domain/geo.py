"""Geohash + bbox helpers (pure)."""
from __future__ import annotations

from dataclasses import dataclass

_BASE32 = "0123456789bcdefghjkmnpqrstuvwxyz"
_DECODE = {c: i for i, c in enumerate(_BASE32)}
_MASKS = (16, 8, 4, 2, 1)


def geohash_encode(lat: float, lon: float, precision: int = 7) -> str:
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    bit, ch, even = 0, 0, True
    out: list[str] = []
    while len(out) < precision:
        if even:
            mid = (lon_lo + lon_hi) / 2
            if lon >= mid:
                ch |= _MASKS[bit]
                lon_lo = mid
            else:
                lon_hi = mid
        else:
            mid = (lat_lo + lat_hi) / 2
            if lat >= mid:
                ch |= _MASKS[bit]
                lat_lo = mid
            else:
                lat_hi = mid
        even = not even
        if bit < 4:
            bit += 1
        else:
            out.append(_BASE32[ch])
            bit, ch = 0, 0
    return "".join(out)


def geohash_bounds(gh: str) -> tuple[float, float, float, float]:
    """(lat_lo, lat_hi, lon_lo, lon_hi) of a geohash cell."""
    lat_lo, lat_hi, lon_lo, lon_hi = -90.0, 90.0, -180.0, 180.0
    even = True
    for c in gh:
        cd = _DECODE[c]
        for mask in _MASKS:
            if even:
                mid = (lon_lo + lon_hi) / 2
                if cd & mask:
                    lon_lo = mid
                else:
                    lon_hi = mid
            else:
                mid = (lat_lo + lat_hi) / 2
                if cd & mask:
                    lat_lo = mid
                else:
                    lat_hi = mid
            even = not even
    return lat_lo, lat_hi, lon_lo, lon_hi


def geohash_center(gh: str) -> tuple[float, float]:
    a, b, c, d = geohash_bounds(gh)
    return (a + b) / 2, (c + d) / 2


def degrade(lat: float, lon: float, precision: str) -> tuple[float, float]:
    """Apply role-dependent location precision: the centre of the geohash cell, or the exact point."""
    if precision == "exact":
        return lat, lon
    n = int(precision.removeprefix("geohash"))
    return geohash_center(geohash_encode(lat, lon, n))


def cell_precision_for_zoom(zoom: int) -> int:
    """Geohash length of the aggregated cells sent at low zoom (< 9)."""
    if zoom <= 4:
        return 3
    if zoom <= 6:
        return 4
    return 5


CELL_ZOOM_THRESHOLD = 9  # below this zoom the map receives aggregated cells instead of points


@dataclass(frozen=True, slots=True)
class BBox:
    west: float
    south: float
    east: float
    north: float

    def contains(self, lat: float, lon: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lon <= self.east

    def center(self) -> tuple[float, float]:
        return (self.south + self.north) / 2, (self.west + self.east) / 2


def parse_bbox(raw: str | list[float] | tuple[float, ...]) -> BBox:
    """west,south,east,north as a string or 4-sequence. Raises ValueError when malformed (caller maps to 422)."""
    parts = [float(x) for x in (raw.split(",") if isinstance(raw, str) else raw)]
    if len(parts) != 4:
        raise ValueError("bbox needs 4 numbers: west,south,east,north")
    w, s, e, n = parts
    if not (-180 <= w <= 180 and -180 <= e <= 180 and -90 <= s <= 90 and -90 <= n <= 90) or w >= e or s >= n:
        raise ValueError("bbox out of range or not ordered west<east, south<north")
    return BBox(w, s, e, n)
