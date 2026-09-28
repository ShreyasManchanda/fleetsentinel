"""Driver scorecards. PII is decrypted only for roles holding driver.pii; erased subjects render as [erased]."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fscore.crypto import AAD_DRIVER_NAME, ERASED_MARKER, CryptoError, decrypt_or_erased

from ..api.context import Ctx
from ..domain.roles import Perm
from ..errors import not_found

log = logging.getLogger("fs_api.drivers")


async def display_name(ctx: Ctx, row: dict[str, Any]) -> str | None:
    if not ctx.can(Perm.DRIVER_PII):
        return None
    if row.get("erased_at") is not None:
        return ERASED_MARKER
    try:
        return await asyncio.to_thread(decrypt_or_erased, ctx.deps.keys, row.get("name_enc"), row.get("wrapped_dek"), AAD_DRIVER_NAME)
    except CryptoError:
        log.warning("driver name could not be decrypted", extra={"driver_id": str(row.get("id"))})
        return None


def _driver_out(row: dict[str, Any], name: str | None, min_km: float) -> dict[str, Any]:
    km = float(row.get("km_30d") or 0.0)
    score = row.get("safety_score")
    conf = row.get("safety_confidence") or ("ok" if km >= min_km else "low")
    return {
        "id": row["id"], "display_name": name, "erased": row.get("erased_at") is not None,
        "safety_score": float(score) if score is not None else 100.0, "confidence": conf, "km_30d": km,
    }


async def list_drivers(ctx: Ctx, *, sort: str, fleet_id: str | None, cursor: str | None, limit: int | None) -> dict[str, Any]:
    n = ctx.limit(limit)
    after = ctx.cursor_in("drivers", cursor)
    async with ctx.tx() as repos:
        rows = await repos.drivers.list_page(sort=sort, fleet_id=fleet_id, after=after, limit=n + 1)
    nxt = None
    if len(rows) > n:
        rows = rows[:n]
        last = rows[-1]
        nxt = ctx.cursor_out("drivers", [float(last["sort_score"]), str(last["id"])])
    names = await asyncio.gather(*(display_name(ctx, r) for r in rows))
    ctx.note_resources("driver")
    return {"items": [_driver_out(r, nm, ctx.deps.settings.min_exposure_km) for r, nm in zip(rows, names, strict=True)], "next_cursor": nxt}


def coaching_levers(per100: dict[str, float], p50: dict[str, float]) -> list[dict[str, Any]]:
    """Top levers = metrics where the driver is worst relative to the fleet median (peer-percentile delta, spec 10.2)."""
    labels = {"harsh_brake": "harsh braking events per 100 km", "harsh_accel": "harsh accelerations per 100 km",
              "overspeed_min": "minutes over the speed limit per 100 km", "night_share": "share of night driving",
              "idle_share": "share of time idling"}
    scored = []
    for k, label in labels.items():
        v, m = float(per100.get(k) or 0.0), float(p50.get(k) or 0.0)
        if v > m and v > 0:
            scored.append((v / m if m > 0 else v + 1.0, k, label, v, m))
    scored.sort(reverse=True)
    return [{"lever": k, "value": round(v, 2), "fleet_p50": round(m, 2),
             "message": f"{label}: {v:.1f} vs fleet median {m:.1f}"} for _, k, label, v, m in scored[:2]]


async def get_driver(ctx: Ctx, driver_id: str) -> dict[str, Any]:
    async with ctx.tx() as repos:
        row = await repos.drivers.get(driver_id)
    if row is None:
        raise not_found("driver")
    name = await display_name(ctx, row)
    out = _driver_out(row, name, ctx.deps.settings.min_exposure_km)
    per100 = row.get("per_100km") or {}
    out["per_100km"] = {k: (round(float(v), 3) if v is not None else None) for k, v in per100.items()}
    out["coaching"] = coaching_levers(per100, row.get("fleet_p50") or {})
    ctx.note_resources("driver", [driver_id])
    return out
