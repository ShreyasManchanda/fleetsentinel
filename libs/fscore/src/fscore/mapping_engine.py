"""Declarative OEM mapping engine (ADR-05). Interprets contracts/mappings/*.yaml documents. See contracts/mappings/README.md.

Safety: mappings are DATA. Transforms come from a fixed allowlist, paths are a restricted JSONPath subset, there is no
eval/exec. A mapping is compiled once into pre-bound closures, so per-record cost is a handful of dict lookups.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import orjson

from . import dtc as dtc_mod
from . import vin as vin_mod
from .contracts import ERR_BAD_ENVELOPE, ERR_CLOCK_SKEW, ERR_VALIDATION_FAILED

CANONICAL_EVENTS = frozenset({
    "POSITION", "HARSH_BRAKE", "HARSH_ACCEL", "IGNITION_ON", "IGNITION_OFF", "TOW", "CRASH",
    "CHARGING_START", "CHARGING_END", "DTC_SET", "OVERSPEED",
})
CANONICAL_FIELDS = frozenset({
    "vin", "ts_ms", "seq", "lat", "lon", "speed_kmh", "soc_pct", "fuel_pct", "odo_km", "dtc", "evt", "ignition",
    "sw_version", "soh_pct", "engine_temp_c", "driver_ref",
})
REQUIRED_DEFAULT = frozenset({"vin", "ts_ms", "lat", "lon"})
MAX_FUTURE_SKEW_MS = 120_000
MIN_TS_MS = 1_577_836_800_000  # 2020-01-01

_MISSING = object()
_TOKEN = re.compile(r"\.([A-Za-z_][\w\-]*)|\['([^']+)'\]|\[(\d+)\]|\[(\*)\]")


class MappingError(ValueError):
    """The mapping document itself is invalid (raised at compile time, never per record)."""


class RecordError(Exception):
    def __init__(self, reason: str, detail: str):
        super().__init__(f"{reason}: {detail}")
        self.reason, self.detail = reason, detail


@dataclass(slots=True)
class RecordFailure:
    reason: str
    detail: str
    raw: Any


@dataclass(slots=True)
class BatchResult:
    oem: str
    mapping_version: int
    batch_id: str | None
    ok: list[dict[str, Any]] = field(default_factory=list)
    failed: list[RecordFailure] = field(default_factory=list)


# ---------------------------------------------------------------- paths
def _parse_path(path: str) -> tuple[bool, tuple[Any, ...]]:
    """Return (is_batch_root, tokens). `$$` addresses the whole batch payload; `$` the current record."""
    if not isinstance(path, str) or not path.startswith("$"):
        raise MappingError(f"invalid path {path!r}")
    batch_root = path.startswith("$$")
    rest = path[2:] if batch_root else path[1:]
    tokens: list[Any] = []
    pos = 0
    while pos < len(rest):
        m = _TOKEN.match(rest, pos)
        if not m:
            raise MappingError(f"unsupported path syntax at {rest[pos:]!r} in {path!r}")
        key, qkey, idx, star = m.groups()
        tokens.append(key or qkey if (key or qkey) else (int(idx) if idx is not None else "*"))
        pos = m.end()
    return batch_root, tuple(tokens)


def _walk(node: Any, tokens: tuple[Any, ...]) -> Any:
    for t in tokens:
        if isinstance(t, int):
            if isinstance(node, list) and -len(node) <= t < len(node):
                node = node[t]
            else:
                return _MISSING
        elif isinstance(node, dict):
            node = node.get(t, _MISSING)
            if node is _MISSING:
                return _MISSING
        else:
            return _MISSING
    return node


# ---------------------------------------------------------------- transforms (allowlist)
def _to_bool(v: Any) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "t", "yes", "y", "on"}
    return bool(v)


def _iso_to_ms(v: Any) -> int:
    dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def _validate_vin(v: Any) -> str:
    s = str(v).strip().upper()
    if not vin_mod.is_valid_vin(s):
        raise RecordError(ERR_VALIDATION_FAILED, f"invalid VIN {s!r}")
    return s


def _split_csv(v: Any) -> list[str]:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [p for p in re.split(r"[,;\s]+", str(v)) if p]


def _make_transform(spec: Any) -> Callable[[Any], Any]:
    simple: dict[str, Callable[[Any], Any]] = {
        "strip": lambda v: str(v).strip(),
        "upper": lambda v: str(v).upper(),
        "lower": lambda v: str(v).lower(),
        "to_float": float,
        "to_int": lambda v: int(float(v)),
        "to_bool": _to_bool,
        "pct_from_fraction": lambda v: float(v) * 100.0,
        "epoch_s_to_ms": lambda v: int(float(v) * 1000),
        "iso8601_to_ms": _iso_to_ms,
        "split_csv": _split_csv,
        "validate_vin": _validate_vin,
        "validate_dtc": lambda v: dtc_mod.parse_dtc_list(v),
    }
    if isinstance(spec, str):
        if spec not in simple:
            raise MappingError(f"unknown transform {spec!r}")
        return simple[spec]
    if isinstance(spec, dict) and len(spec) == 1:
        (name, arg), = spec.items()
        if name == "mul":
            k = float(arg)
            return lambda v: float(v) * k
        if name == "add":
            k = float(arg)
            return lambda v: float(v) + k
        if name == "round":
            n = int(arg)
            return lambda v: round(float(v), n)
        if name == "clamp":
            lo, hi = float(arg[0]), float(arg[1])
            return lambda v: min(max(float(v), lo), hi)
        if name == "enum_map":
            table = {str(k): str(v) for k, v in arg.items()}
            bad = set(table.values()) - CANONICAL_EVENTS
            if bad:
                raise MappingError(f"enum_map targets not canonical events: {sorted(bad)}")

            def _map_enum(v: Any, _table: dict[str, str] = table) -> str:
                s = str(v)
                if not s:
                    return "POSITION"  # empty/missing event code = plain position update
                try:
                    return _table[s]
                except KeyError:
                    # Unknown codes (typo, new firmware event, schema drift) must NOT
                    # be silently reclassified as POSITION: they fail the record so it
                    # lands in the DLQ and feeds drift detection (B-021).
                    raise RecordError(ERR_VALIDATION_FAILED, f"unmapped event code {s!r}") from None

            return _map_enum
    raise MappingError(f"unsupported transform spec {spec!r}")


# ---------------------------------------------------------------- compiled mapping
@dataclass(slots=True)
class _Rule:
    name: str
    getters: tuple[tuple[bool, tuple[Any, ...]], ...]   # (batch_root, tokens) alternatives
    const: Any
    transforms: tuple[Callable[[Any], Any], ...]
    default: Any
    required: bool


class Mapping:
    def __init__(self, doc: dict[str, Any]):
        self.doc = doc
        try:
            self.oem: str = doc["oem"]
            self.version: int = int(doc["version"])
            env = doc["envelope"]
            fields = doc["fields"]
        except (KeyError, TypeError, ValueError) as e:
            raise MappingError(f"missing/invalid top-level key: {e}") from e
        records_batch_root, rec_tokens = _parse_path(env["records"])
        if not rec_tokens or rec_tokens[-1] != "*":
            raise MappingError("envelope.records must end with [*]")
        self._records_root = (records_batch_root, rec_tokens[:-1])
        self._batch_id = _parse_path(env["batch_id"]) if env.get("batch_id") else None
        self._columns = _parse_path(env["columns"]) if env.get("columns") else None
        unknown = set(fields) - CANONICAL_FIELDS
        if unknown:
            raise MappingError(f"unknown canonical fields: {sorted(unknown)}")
        for req in REQUIRED_DEFAULT:
            if req not in fields:
                raise MappingError(f"required canonical field not mapped: {req}")
        self._rules = tuple(self._compile(n, r) for n, r in fields.items())

    @staticmethod
    def _compile(name: str, rule: dict[str, Any]) -> _Rule:
        alts = rule.get("any") or ([rule["path"]] if "path" in rule else [])
        if not alts and "const" not in rule:
            raise MappingError(f"field {name}: needs path, any or const")
        getters = tuple(_parse_path(p) for p in alts)
        for _, toks in getters:
            if "*" in toks:
                raise MappingError(f"field {name}: wildcard only allowed in envelope.records")
        return _Rule(
            name=name,
            getters=getters,
            const=rule.get("const", _MISSING),
            transforms=tuple(_make_transform(t) for t in rule.get("transform", [])),
            default=rule.get("default", _MISSING),
            required=bool(rule.get("required", name in REQUIRED_DEFAULT)),
        )

    # ---- public
    @classmethod
    def from_yaml(cls, text: str) -> Mapping:
        import yaml

        return cls(yaml.safe_load(text))

    def apply(self, payload: bytes | str | dict[str, Any], now_ms: int | None = None) -> BatchResult:
        """Normalize one batch payload. Never raises for bad data: bad records are returned in `failed`."""
        if isinstance(payload, (bytes, str)):
            try:
                payload = orjson.loads(payload)
            except orjson.JSONDecodeError as e:
                res = BatchResult(self.oem, self.version, None)
                res.failed.append(RecordFailure(ERR_BAD_ENVELOPE, f"not JSON: {e}", None))
                return res
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        batch_id = None
        if self._batch_id:
            v = _walk(payload, self._batch_id[1])
            batch_id = None if v is _MISSING else str(v)
        res = BatchResult(self.oem, self.version, batch_id)
        records = _walk(payload, self._records_root[1])
        if not isinstance(records, list):
            res.failed.append(RecordFailure(ERR_BAD_ENVELOPE, "records path did not resolve to a list", None))
            return res
        columns = None
        if self._columns:
            columns = _walk(payload, self._columns[1])
            if not isinstance(columns, list):
                res.failed.append(RecordFailure(ERR_BAD_ENVELOPE, "columns path did not resolve to a list", None))
                return res
        for raw in records:
            try:
                rec = raw
                if columns is not None:
                    if not isinstance(raw, list) or len(raw) != len(columns):
                        raise RecordError(ERR_BAD_ENVELOPE, "row/columns length mismatch")
                    rec = dict(zip(columns, raw, strict=True))
                res.ok.append(self._map_record(rec, payload, now))
            except RecordError as e:
                res.failed.append(RecordFailure(e.reason, e.detail, raw))
            except (ValueError, TypeError, OverflowError) as e:
                res.failed.append(RecordFailure(ERR_VALIDATION_FAILED, f"{type(e).__name__}: {e}", raw))
        return res

    # ---- internals
    def _map_record(self, rec: Any, batch: Any, now_ms: int) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for rule in self._rules:
            v: Any = _MISSING
            if rule.const is not _MISSING:
                v = rule.const
            else:
                for batch_root, toks in rule.getters:
                    v = _walk(batch if batch_root else rec, toks)
                    if v is not _MISSING and v is not None:
                        break
                    v = _MISSING
            if v is _MISSING:
                if rule.default is not _MISSING:
                    v = rule.default
                elif rule.required:
                    raise RecordError(ERR_VALIDATION_FAILED, f"missing required field {rule.name}")
                else:
                    continue
            for tf in rule.transforms:
                v = tf(v)
            out[rule.name] = v
        out.setdefault("evt", "POSITION")
        out.setdefault("dtc", [])
        out.setdefault("seq", 0)
        out["oem"] = self.oem
        return self._validate(out, now_ms)

    @staticmethod
    def _validate(o: dict[str, Any], now_ms: int) -> dict[str, Any]:
        if not (-90.0 <= o["lat"] <= 90.0 and -180.0 <= o["lon"] <= 180.0):
            raise RecordError(ERR_VALIDATION_FAILED, "lat/lon out of range")
        ts = o["ts_ms"]
        if ts > now_ms + MAX_FUTURE_SKEW_MS:
            raise RecordError(ERR_CLOCK_SKEW, f"ts {ts} is more than 2 min in the future")
        if ts < MIN_TS_MS:
            raise RecordError(ERR_VALIDATION_FAILED, f"ts {ts} before 2020")
        for k, lo, hi in (("soc_pct", 0, 100), ("fuel_pct", 0, 100), ("speed_kmh", 0, 350)):
            if k in o and not (lo <= o[k] <= hi):
                raise RecordError(ERR_VALIDATION_FAILED, f"{k} out of range")
        if "ignition" not in o:
            o["ignition"] = o.get("speed_kmh", 0.0) > 0.0
        return o


def to_proto(rec: dict[str, Any], ingest_ts_ms: int, driver_ref_enc: bytes = b""):
    """Canonical dict -> fleetsentinel.v1.TelemetryEvent."""
    from fleetsentinel.v1 import telemetry_pb2 as pb

    e = pb.TelemetryEvent(
        vin=rec["vin"], ts_ms=int(rec["ts_ms"]), seq=int(rec.get("seq", 0)), lat=rec["lat"], lon=rec["lon"],
        speed_kmh=rec.get("speed_kmh", 0.0), odo_km=rec.get("odo_km", 0.0), dtc=rec.get("dtc", []),
        evt=getattr(pb, rec.get("evt", "POSITION")), ignition=bool(rec.get("ignition", False)),
        sw_version=rec.get("sw_version", ""), oem=rec.get("oem", ""), ingest_ts_ms=ingest_ts_ms,
        driver_ref_enc=driver_ref_enc,
    )
    for k in ("soc_pct", "fuel_pct", "soh_pct", "engine_temp_c"):
        if k in rec:
            setattr(e, k, rec[k])
    return e
