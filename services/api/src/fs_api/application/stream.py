"""WebSocket stream session: subscription state and 1 Hz delta computation (transport lives in the router)."""
from __future__ import annotations

import base64
import struct
from typing import Any

from ..container import Deps
from ..domain.geo import CELL_ZOOM_THRESHOLD, cell_precision_for_zoom, degrade, parse_bbox
from ..domain.roles import Purpose, Role, location_precision
from ..security.jwt_auth import Principal
from .vehicles import aggregate_cells

STATUS_CODE = {"PARKED": 1, "DRIVING": 2, "IDLING": 3, "CHARGING": 4, "OFFLINE": 5, "TOWED": 6}
STRIDE = 3  # float32 values per vehicle: [lat, lon, status_code]
MAX_POINTS = 5000
VALID_TOPICS = {"alerts"}


def pack_positions(items: list[tuple[float, float, str | None]]) -> str:
    """base64 of a little-endian Float32Array laid out [lat, lon, status, lat, lon, status, ...]."""
    flat: list[float] = []
    for lat, lon, status in items:
        flat += [lat, lon, float(STATUS_CODE.get(status or "", 0))]
    return base64.b64encode(struct.pack(f"<{len(flat)}f", *flat)).decode()


def unpack_positions(data: str) -> list[tuple[float, float, int]]:
    raw = base64.b64decode(data)
    vals = struct.unpack(f"<{len(raw) // 4}f", raw)
    return [(vals[i], vals[i + 1], int(vals[i + 2])) for i in range(0, len(vals), STRIDE)]


class StreamSession:
    def __init__(self, deps: Deps, principal: Principal, purpose: Purpose = Purpose.OPERATIONS):
        self.deps, self.principal, self.purpose = deps, principal, purpose
        self.bbox = None
        self.zoom = 12
        self.topics: set[str] = set()
        self._sent: dict[str, tuple[float, float, str | None]] = {}
        self._sent_cells: list[dict[str, Any]] | None = None
        self._ticks = 0
        self.errors = 0

    @property
    def wants_alerts(self) -> bool:
        return "alerts" in self.topics

    def precision(self, flagged: bool = False) -> str:
        return location_precision(self.principal.roles, self.purpose, flagged)

    def handle(self, msg: Any) -> dict[str, Any] | None:
        """Process a client message; returns an error/ack message to send, or None."""
        if not isinstance(msg, dict) or msg.get("type") != "subscribe":
            self.errors += 1
            return {"type": "error", "detail": "expected {type: subscribe, ...}"}
        topics = msg.get("topics")
        if topics is not None:
            if not isinstance(topics, list) or not set(topics) <= VALID_TOPICS:
                self.errors += 1
                return {"type": "error", "detail": "unknown topic"}
            self.topics = set(topics)
        if "bbox" in msg:
            try:
                zoom = int(msg.get("zoom", 12))
                if not 1 <= zoom <= 20:
                    raise ValueError("zoom must be 1..20")
                self.bbox = parse_bbox(msg["bbox"])
            except (ValueError, TypeError) as e:
                self.errors += 1
                return {"type": "error", "detail": str(e)}
            self.zoom = zoom
            self._sent, self._sent_cells = {}, None  # a new viewport starts with a full snapshot
        return {"type": "subscribed", "topics": sorted(self.topics), "zoom": self.zoom, "precision": self.precision()}

    async def tick(self, now_ms: int) -> list[dict[str, Any]]:
        """One push cycle: position/cell deltas for the viewport, plus a freshness beacon every 5 ticks."""
        out: list[dict[str, Any]] = []
        self._ticks += 1
        if self.bbox is not None:
            b = self.bbox
            states, truncated = await self.deps.state.viewport(self.principal.tenant_id, b.west, b.south, b.east, b.north, MAX_POINTS)
            if self.zoom < CELL_ZOOM_THRESHOLD:
                cells = aggregate_cells(states, cell_precision_for_zoom(self.zoom))
                if cells != self._sent_cells:
                    self._sent_cells = cells
                    out.append({"type": "cells", "ts": now_ms, "cells": cells, "truncated": truncated})
            else:
                flagged: set[str] = set()
                if self.purpose is Purpose.RECOVERY and Role.LENDER_AGENT in self.principal.roles:
                    flagged = await self.deps.state.flagged_set(self.principal.tenant_id)
                current: dict[str, tuple[float, float, str | None]] = {}
                for st in states:
                    lat, lon = degrade(st["lat"], st["lon"], self.precision(st["vin"] in flagged))
                    current[st["vin"]] = (round(lat, 6), round(lon, 6), st.get("status"))
                first = not self._sent
                changed = [(v, p) for v, p in current.items() if self._sent.get(v) != p]
                removed = [v for v in self._sent if v not in current]
                if changed or removed:
                    out.append({
                        "type": "positions", "ts": now_ms, "snapshot": first, "stride": STRIDE, "vins": [v for v, _ in changed],
                        "data": pack_positions([p for _, p in changed]), "removed": removed, "truncated": truncated,
                    })
                self._sent = current
        if self._ticks % 5 == 1:
            out.append({"type": "freshness", "p95_ms": await self.deps.state.freshness_p95_ms()})
        return out
