"""Idling cost from time-of-use tariff rows (mirrors tariff_rate: hour_from inclusive, hour_to exclusive, local hours)."""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class TariffRow:
    energy_type: str        # DIESEL | PETROL | ELECTRICITY
    hour_from: int          # 0..23
    hour_to: int            # 1..24 (exclusive)
    price_per_unit: float   # USD per L (fuel) or kWh (electricity)
    unit: str = "L"


@dataclass(frozen=True, slots=True)
class IdleCost:
    seconds: float
    quantity: float         # litres or kWh burned
    cost_usd: float


def price_at(rows: Sequence[TariffRow], energy_type: str, hour: int) -> float | None:
    """Price for a local hour, or None if no row covers it. O(rows)."""
    for r in rows:
        if r.energy_type == energy_type and r.hour_from <= hour < r.hour_to:
            return r.price_per_unit
    return None


def _fallback_price(rows: Sequence[TariffRow], energy_type: str) -> float:
    prices = [r.price_per_unit for r in rows if r.energy_type == energy_type]
    return sum(prices) / len(prices) if prices else 0.0


def idling_cost(
    start_ms: int,
    end_ms: int,
    rows: Sequence[TariffRow],
    energy_type: str,
    burn_per_hour: float,
    tz_offset_min: int = 0,
) -> IdleCost:
    """Cost of idling over [start_ms, end_ms): the interval is cut at local hour boundaries and each piece is priced
    with the tariff of its hour. ``burn_per_hour`` is L/h for fuel, kW for electricity (HVAC idle draw).
    Hours not covered by any row use the mean price of that energy type. O(hours spanned)."""
    if end_ms <= start_ms or burn_per_hour < 0:
        return IdleCost(0.0, 0.0, 0.0)
    fallback = _fallback_price(rows, energy_type)
    off = tz_offset_min * 60_000
    hour_ms = 3_600_000
    cost = qty = 0.0
    t = start_ms
    while t < end_ms:
        local = t + off
        boundary = (local // hour_ms + 1) * hour_ms - off
        seg_end = min(end_ms, boundary)
        hour = (local // hour_ms) % 24
        price = price_at(rows, energy_type, hour)
        piece_qty = burn_per_hour * (seg_end - t) / hour_ms
        cost += piece_qty * (fallback if price is None else price)
        qty += piece_qty
        t = seg_end
    return IdleCost((end_ms - start_ms) / 1000.0, qty, cost)
