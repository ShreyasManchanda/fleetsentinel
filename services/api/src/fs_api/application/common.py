"""Shared helpers for application services: row->model mapping, location precision, pseudonyms, risk decisioning."""
from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime
from typing import Any

from ..api.context import Ctx
from ..config import Settings
from ..domain.geo import degrade
from ..domain.roles import Purpose, Role

DRIVER_KEYS = {"driver", "driver_ref", "driver_ref_enc", "driver_id", "driver_name"}


def iso_from_ms(ms: int | float | None) -> datetime | None:
    if not ms:
        return None
    return datetime.fromtimestamp(float(ms) / 1000.0, tz=UTC)


def pseudonym(secret: str, driver_id: Any) -> str | None:
    """Stable pseudonymous driver token; never reversible without the server secret, and unrelated to the erased identity."""
    if driver_id is None:
        return None
    return "drv_" + hmac.new(secret.encode(), str(driver_id).encode(), hashlib.sha256).hexdigest()[:12]


async def flagged_lookup(ctx: Ctx) -> set[str]:
    """VINs the lender flagged (in default). Only consulted for RECOVERY by a lender_agent, the sole purpose that can unlock them."""
    if ctx.purpose is Purpose.RECOVERY and Role.LENDER_AGENT in ctx.principal.roles:
        return await ctx.deps.state.flagged_set(ctx.tenant_id)
    return set()


def precision_for(ctx: Ctx, vin: str, flagged: set[str]) -> str:
    return ctx.precision(vin in flagged)


def masked_location(lat: float | None, lon: float | None, precision: str) -> tuple[float | None, float | None]:
    if lat is None or lon is None:
        return None, None
    a, b = degrade(float(lat), float(lon), precision)
    return round(a, 6), round(b, 6)


def sanitize_twin(twin: dict[str, Any] | None, precision: str) -> dict[str, Any] | None:
    """The Mongo digital twin is schema-variable: strip driver linkage and degrade any embedded coordinates."""
    if twin is None:
        return None

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {k: walk(v) for k, v in node.items() if k not in DRIVER_KEYS and k != "_id"}
            if "lat" in out and "lon" in out and isinstance(out["lat"], (int, float)) and isinstance(out["lon"], (int, float)):
                out["lat"], out["lon"] = masked_location(out["lat"], out["lon"], precision)
            return out
        if isinstance(node, list):
            return [walk(x) for x in node]
        return node

    return walk(twin)


def risk_detail(row: dict[str, Any] | None, s: Settings) -> dict[str, Any] | None:
    """EV = p * C_breakdown - C_inspection (spec 10.1). Recommend INSPECT when EV > 0, MONITOR when within 2x below break-even."""
    if row is None:
        return None
    p = float(row["p_failure_7d"])
    ev = p * s.cost_breakdown_usd - s.cost_inspection_usd
    breakeven = s.cost_inspection_usd / s.cost_breakdown_usd
    rec = "INSPECT" if ev > 0 else ("MONITOR" if p >= breakeven / 2 else "NONE")
    name = str(row.get("model_name") or "")
    ver = str(row.get("model_version") or "")
    codes = []
    for c in row.get("reason_codes") or []:
        if isinstance(c, dict) and "feature" in c and "contribution" in c:
            codes.append({"feature": str(c["feature"]), "contribution": float(c["contribution"]), "human": c.get("human")})
    return {
        "p_failure_7d": p,
        "model_version": f"{name}:{ver}" if name else ver,
        "fallback": name.startswith("rule") or "rule" in ver,
        "reason_codes": codes,
        "expected_value_usd": round(ev, 2),
        "recommendation": rec,
        "predicted_at": row.get("predicted_at"),
    }


def alert_out(row: dict[str, Any], lat: float | None = None, lon: float | None = None) -> dict[str, Any]:
    latency = None
    if row.get("detected_ts") is not None and row.get("ingest_ts") is not None:
        latency = max(0, int((row["detected_ts"] - row["ingest_ts"]).total_seconds() * 1000))
    return {
        "id": row["id"], "vin": str(row["vin"]).strip(), "rule": row["rule_code"], "severity": row["severity"],
        "status": row["status"], "occurrences": row.get("occurrences"), "summary": row.get("summary"),
        "details": row.get("details") or {}, "impact_usd": float(row["impact_at_creation"]) if row.get("impact_at_creation") is not None else None,
        "opened_at": row["opened_at"], "event_ts": row.get("event_ts"), "detected_ts": row.get("detected_ts"),
        "latency_ms": latency, "lat": lat, "lon": lon, "trace_id": row.get("trace_id"),
    }
