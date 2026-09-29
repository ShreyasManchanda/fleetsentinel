"""Checkpoint codec: bulk snapshots <-> JSON-safe dicts for Redis (atomic MULTI/EXEC in the adapter).

The adapter writes the encoded dict plus ``last_offset`` in one MULTI/EXEC and only then
commits the Kafka offset, giving effectively-once state on top of at-least-once delivery.
"""
from __future__ import annotations

import base64
from typing import Any


def b64e(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def b64d(s: str) -> bytes:
    return base64.b64decode(s.encode("ascii"))


def encode_snapshot(snap: dict[str, Any]) -> dict[str, Any]:
    """Copy a bulk snapshot, base64-encoding the binary sketch blobs. JSON-safe."""
    out = dict(snap)
    cms = dict(snap.get("cms", {}))
    if isinstance(cms.get("table"), bytes):
        cms["table"] = b64e(cms["table"])
    out["cms"] = cms
    out["hll"] = {cell: b64e(blob) for cell, blob in snap.get("hll", {}).items()}
    return out


def decode_snapshot(data: dict[str, Any]) -> dict[str, Any]:
    out = dict(data)
    cms = dict(data.get("cms", {}))
    if isinstance(cms.get("table"), str):
        cms["table"] = b64d(cms["table"])
    out["cms"] = cms
    out["hll"] = {cell: b64d(blob) for cell, blob in data.get("hll", {}).items()}
    return out
