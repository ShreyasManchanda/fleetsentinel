"""Vehicle, fleet and map use cases."""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from ..api.context import Ctx
from ..domain.geo import CELL_ZOOM_THRESHOLD, cell_precision_for_zoom, geohash_encode, parse_bbox
from ..errors import not_found, unprocessable
from ..infra_errors import is_infra_error
from .common import (
    alert_out,
    flagged_lookup,
    iso_from_ms,
    masked_location,
    precision_for,
    pseudonym,
    risk_detail,
    sanitize_twin,
)

log = logging.getLogger("fs_api.vehicles")
MAX_SCAN_ROUNDS = 5
MAP_POINT_LIMIT = 5000


def _vehicle_cursor(sort: str, row: dict[str, Any]) -> list[Any]:
    return [float(row["last_risk_score"]), row["vin"]] if sort == "risk_desc" else [row["vin"]]


def _merge_live(row: dict[str, Any], live: dict[str, Any] | None, precision: str) -> dict[str, Any]:
    out = {
        "vin": str(row["vin"]).strip(), "fleet_id": row["fleet_id"], "oem": row["oem"], "model": row["model"],
        "powertrain": row["powertrain"], "last_risk_score": float(row["last_risk_score"]) if row.get("last_risk_score") is not None else None,
        "open_alerts": int(row.get("open_alerts") or 0),
    }
    if live:
        out["status"] = live.get("status")
        out["last_seen"] = iso_from_ms(live.get("ts_ms"))
        out["lat"], out["lon"] = masked_location(live.get("lat"), live.get("lon"), precision)
    return out


async def list_vehicles(ctx: Ctx, *, fleet_id: str | None, status: str | None, min_risk: float | None, vin_prefix: str | None,
                        sort: str, cursor: str | None, limit: int | None) -> dict[str, Any]:
    n = ctx.limit(limit)
    after = ctx.cursor_in("vehicles", cursor)
    flagged = await flagged_lookup(ctx)
    fetch = n + 1 if not status else (n + 1) * 3
    items: list[dict[str, Any]] = []
    next_cursor: str | None = None
    async with ctx.tx() as repos:
        cur_after, exhausted, rounds = after, False, 0
        while not exhausted and rounds < MAX_SCAN_ROUNDS and len(items) <= n:
            rows = await repos.vehicles.list_page(fleet_id=fleet_id, min_risk=min_risk, vin_prefix=vin_prefix.upper() if vin_prefix else None,
                                                  sort=sort, after=cur_after, limit=fetch)
            rounds += 1
            exhausted = len(rows) < fetch
            live = await _states(ctx, [str(r["vin"]).strip() for r in rows])
            for r in rows:
                vin = str(r["vin"]).strip()
                lv = live.get(vin)
                if status and (lv or {}).get("status") != status:
                    continue
                r["_cursor"] = _vehicle_cursor(sort, r)
                items.append(_merge_live(r, lv, precision_for(ctx, vin, flagged)) | {"_cursor": r["_cursor"]})
                if len(items) > n:
                    break
            if rows:
                cur_after = _vehicle_cursor(sort, rows[-1])
        if len(items) > n:
            items = items[:n]
            next_cursor = ctx.cursor_out("vehicles", items[-1]["_cursor"])
        elif not exhausted and cur_after is not None:
            next_cursor = ctx.cursor_out("vehicles", cur_after)  # scan cap reached: let the client continue
    for it in items:
        it.pop("_cursor", None)
    ctx.note_resources("vehicle")
    return {"items": items, "next_cursor": next_cursor}


async def _states(ctx: Ctx, vins: list[str]) -> dict[str, dict[str, Any]]:
    """Live state is enrichment: if Redis is down the list still works (without status/position)."""
    if not vins:
        return {}
    try:
        return await ctx.deps.state.get_states(ctx.tenant_id, vins)
    except Exception as e:
        if not is_infra_error(e) and type(e).__name__ != "CircuitOpen":
            raise
        log.warning("state store unavailable, serving vehicles without live state")
        return {}


async def vehicle_360(ctx: Ctx, vin: str) -> dict[str, Any]:
    flagged = await flagged_lookup(ctx)
    precision = precision_for(ctx, vin, flagged)
    async with ctx.tx() as repos:
        row = await repos.vehicles.get(vin)
        if row is None:
            raise not_found("vehicle")
        alerts = await repos.vehicles.recent_alerts(vin, 5)
        trips = await repos.vehicles.recent_trips(vin, 5)
        risk = await repos.vehicles.latest_risk(vin)
    live = (await _states(ctx, [vin])).get(vin)
    twin = None
    try:
        twin = await ctx.deps.twins.get(ctx.tenant_id, vin)
    except Exception as e:  # twin is optional: degrade, do not 5xx
        if not is_infra_error(e) and type(e).__name__ != "CircuitOpen":
            raise
        log.warning("twin store unavailable")
    ctx.note_resources("vehicle", [vin])
    secret = ctx.deps.settings.cursor_secret
    out = {
        "vehicle": _merge_live(row, live, precision),
        "twin": sanitize_twin(twin, precision),
        "risk": risk_detail(risk, ctx.deps.settings),
        "recent_alerts": [alert_out(a, *masked_location(live.get("lat") if live else None, live.get("lon") if live else None, precision))
                          for a in alerts],
        "recent_trips": [_trip_out(t, secret) for t in trips],
        "location_precision": precision,
    }
    return out


def _trip_out(t: dict[str, Any], secret: str) -> dict[str, Any]:
    return {
        "id": t["id"], "start_ts": t["start_ts"], "end_ts": t["end_ts"], "distance_km": float(t["distance_km"]),
        "idle_s": t.get("idle_s"), "energy_used": float(t["energy_used"]) if t.get("energy_used") is not None else None,
        "energy_cost": float(t["energy_cost"]) if t.get("energy_cost") is not None else None,
        "harsh_brake_count": t.get("harsh_brake_count"), "harsh_accel_count": t.get("harsh_accel_count"),
        "driver_ref": pseudonym(secret, t.get("driver_id")),
    }


async def vehicle_trips(ctx: Ctx, vin: str, cursor: str | None, limit: int | None) -> dict[str, Any]:
    n = ctx.limit(limit)
    after = ctx.cursor_in(f"trips:{vin}", cursor)
    async with ctx.tx() as repos:
        rows = await repos.vehicles.trips(vin, after, n + 1)
    if rows is None:
        raise not_found("vehicle")
    nxt = None
    if len(rows) > n:
        rows = rows[:n]
        nxt = ctx.cursor_out(f"trips:{vin}", [rows[-1]["start_ts"].isoformat(), str(rows[-1]["id"])])
    ctx.note_resources("trip", [vin])
    secret = ctx.deps.settings.cursor_secret
    return {"items": [_trip_out(t, secret) for t in rows], "next_cursor": nxt}


async def vehicle_risk(ctx: Ctx, vin: str) -> dict[str, Any]:
    async with ctx.tx() as repos:
        if await repos.vehicles.get(vin) is None:
            raise not_found("vehicle")
        risk = await repos.vehicles.latest_risk(vin)
    if risk is None:
        raise not_found("risk prediction")
    ctx.note_resources("vehicle", [vin])
    return risk_detail(risk, ctx.deps.settings)  # type: ignore[return-value]


async def vehicle_lookalikes(ctx: Ctx, vin: str, k: int) -> list[dict[str, Any]]:
    async with ctx.tx() as repos:
        rows = await repos.vehicles.lookalikes(vin, k)
    if rows is None:
        raise not_found("vehicle")
    ctx.note_resources("vehicle", [vin])
    out = []
    for r in rows:
        f = r.get("failed_7d")
        out.append({"vin": str(r["vin"]).strip(), "similarity": round(float(r["similarity"]), 4), "failed_within_7d": f,
                    "note": "matched a pre-failure signature" if f else None})
    return out


async def fleet_summary(ctx: Ctx, fleet_id: str | None) -> dict[str, Any]:
    s = ctx.deps.settings
    threshold = s.cost_inspection_usd / s.cost_breakdown_usd
    async with ctx.tx() as repos:
        row = await repos.vehicles.fleet_summary(fleet_id, threshold)
    counts: dict[str, int] = {}
    freshness, fallback = 0, False
    try:
        counts = await ctx.deps.state.status_counts(ctx.tenant_id)
        freshness = await ctx.deps.state.freshness_p95_ms()
        fallback = await ctx.deps.state.model_fallback_active()
    except Exception as e:
        if not is_infra_error(e) and type(e).__name__ != "CircuitOpen":
            raise
        log.warning("state store unavailable, summary served without live counters")
    return {
        "vehicles_total": int(row["vehicles_total"]),
        "vehicles_by_status": counts,
        "leaking_usd_today": round(float(row["leaking_usd_today"]), 2),
        "savings_opportunity_usd_week": round(float(row["savings_opportunity_usd_week"]), 2),
        "at_risk_vehicles": int(row["at_risk_vehicles"]),
        "open_critical_alerts": int(row["open_critical_alerts"]),
        "freshness_p95_ms": int(freshness),
        "model_fallback_active": fallback,
        "as_of": datetime.now(UTC),
    }


async def map_viewport(ctx: Ctx, bbox_raw: str, zoom: int) -> dict[str, Any]:
    try:
        bbox = parse_bbox(bbox_raw)
    except ValueError as e:
        raise unprocessable(str(e), "invalid-bbox") from e
    flagged = await flagged_lookup(ctx)
    states, truncated = await ctx.deps.state.viewport(ctx.tenant_id, bbox.west, bbox.south, bbox.east, bbox.north, MAP_POINT_LIMIT)
    ctx.note_resources("map")
    if zoom < CELL_ZOOM_THRESHOLD:
        return {"mode": "cells", "cells": aggregate_cells(states, cell_precision_for_zoom(zoom)), "truncated": truncated}
    pts = []
    for st in states:
        lat, lon = masked_location(st["lat"], st["lon"], precision_for(ctx, st["vin"], flagged))
        pts.append({"vin": st["vin"], "lat": lat, "lon": lon, "status": st.get("status")})
    return {"mode": "points", "points": pts, "truncated": truncated}


def aggregate_cells(states: list[dict[str, Any]], precision: int) -> list[dict[str, Any]]:
    """Geohash aggregation; a cell never reveals more than count + idle, so it is safe at any role precision."""
    cells: dict[str, list[int]] = {}
    for st in states:
        c = cells.setdefault(geohash_encode(st["lat"], st["lon"], precision), [0, 0])
        c[0] += 1
        if st.get("status") == "IDLING":
            c[1] += 1
    return [{"geohash": g, "count": v[0], "idle": v[1]} for g, v in sorted(cells.items())]
