"""Vectorised, column-oriented fast path for :class:`fscore.mapping_engine.Mapping` (batch in, canonical dicts out).

It REUSES the compiled Mapping (rules, transforms, validation constants) and must give results identical to
``Mapping.apply``; a differential test proves it. Design:

* Values are extracted one COLUMN at a time (one list comprehension per field instead of a rule loop per record).
* Numeric transform chains (to_float, mul, add, clamp, pct_from_fraction, epoch_s_to_ms, to_int) run on numpy arrays;
  ``round`` stays a Python call so rounding is bit-identical to the reference.
* Discrete transforms (strip/upper/validate_vin/validate_dtc/enum_map/to_bool/split_csv) are memoised across batches
  on the raw value (VINs, DTC strings and event codes repeat heavily), keyed by (type, value) so 1 / True / 1.0 never
  collide.
* Range validation is done with numpy masks. ANY record that would not be a clean success (missing required field,
  transform error, failed validation, odd types) is re-run through the reference per-record code, so failure reasons
  and details are identical by construction. If more than 30% of a batch is bad the whole batch uses the reference.

Complexity: O(records x fields) with a much smaller constant than the per-record interpreter; memo caches are bounded.
"""
from __future__ import annotations

import time
from typing import Any

import numpy as np
import orjson

from . import mapping_engine as me
from .contracts import ERR_BAD_ENVELOPE, ERR_VALIDATION_FAILED

_M = me._MISSING
_NUMT = frozenset({int, float})
_CATCH = (me.RecordError, ValueError, TypeError, OverflowError)
_NUMERIC_STEPS = {"to_float", "pct_from_fraction", "epoch_s_to_ms", "to_int", "mul", "add", "clamp", "round"}
_NO_MEMO = {"iso8601_to_ms"}
_MAX_BAD_FRACTION = 0.30
_I64 = 9.2e18


class _L(tuple):
    """Cached list result (stored immutable, returned as a fresh list)."""


class _Memo:
    def __init__(self, fn, cap: int = 200_000) -> None:
        self.fn, self.cap, self.cache = fn, cap, {}

    def map(self, vals: list, failed: set) -> list:
        cache, fn = self.cache, self.fn
        out = [None] * len(vals)
        for i, v in enumerate(vals):
            k = v if type(v) is str else (type(v), v)
            try:
                r = cache.get(k, _M)
            except TypeError:                       # unhashable input (e.g. a list): no caching
                try:
                    out[i] = fn(v)
                except _CATCH:
                    failed.add(i)
                continue
            if r is _M:
                try:
                    r = fn(v)
                except _CATCH:
                    failed.add(i)
                    continue
                cache[k] = _L(r) if type(r) is list else r
                if len(cache) > self.cap:
                    cache.clear()
            out[i] = list(r) if type(r) is _L else r
        return out


def _elementwise(fn, vals, failed: set) -> list:
    if isinstance(vals, np.ndarray):
        vals = vals.tolist()
    try:
        return [fn(v) for v in vals]
    except _CATCH:
        pass
    out = []
    for i, v in enumerate(vals):
        try:
            out.append(fn(v))
        except _CATCH:
            failed.add(i)
            out.append(None)
    return out


def _as_float(vals) -> np.ndarray | None:
    if isinstance(vals, np.ndarray):
        return vals.astype(np.float64, copy=False)
    if set(map(type, vals)) <= _NUMT:
        try:
            return np.array(vals, dtype=np.float64)
        except OverflowError:
            return None
    return None


def _to_int64(x: np.ndarray) -> np.ndarray | None:
    if x.size == 0:
        return x.astype(np.int64)
    if not np.isfinite(x).all() or np.abs(x).max() >= _I64:
        return None
    return np.trunc(x).astype(np.int64)


class _Step:
    """One transform step: numeric (vectorised) or discrete (memoised / elementwise)."""

    def __init__(self, spec: Any) -> None:
        self.spec = spec
        self.ref = me._make_transform(spec)      # exact reference closure (also the fallback)
        name, arg = (spec, None) if isinstance(spec, str) else next(iter(spec.items()))
        self.name, self.arg = name, arg
        self.numeric = name in _NUMERIC_STEPS
        self.memo = None if (self.numeric or name in _NO_MEMO) else _Memo(self.ref)
        if name == "clamp":
            self.lo, self.hi = float(arg[0]), float(arg[1])
        elif name in ("mul", "add"):
            self.k = float(arg)
        elif name == "round":
            self.n = int(arg)

    def run(self, vals, failed: set):
        if self.memo is not None:
            return self.memo.map(vals if isinstance(vals, list) else vals.tolist(), failed)
        if not self.numeric:
            return _elementwise(self.ref, vals, failed)
        arr = _as_float(vals)
        if arr is None:
            return _elementwise(self.ref, vals, failed)
        name = self.name
        if name == "to_float":
            return arr
        if name == "mul":
            return arr * self.k
        if name == "add":
            return arr + self.k
        if name == "pct_from_fraction":
            return arr * 100.0
        if name == "clamp":
            return np.minimum(np.maximum(arr, self.lo), self.hi)
        if name == "round":
            n = self.n
            return [round(x, n) for x in arr.tolist()]
        x = arr * 1000.0 if name == "epoch_s_to_ms" else arr        # epoch_s_to_ms | to_int
        out = _to_int64(x)
        return _elementwise(self.ref, vals, failed) if out is None else out


class FastMapping:
    def __init__(self, mapping: me.Mapping) -> None:
        self.m = mapping
        self.oem, self.version = mapping.oem, mapping.version
        specs = mapping.doc["fields"]
        self._rules = mapping._rules
        self._steps = [[_Step(s) for s in specs[r.name].get("transform", [])] for r in self._rules]
        self._names = [r.name for r in self._rules]
        self._has = set(self._names)

    # ------------------------------------------------------------------ public
    def apply(self, payload: bytes | str | dict, now_ms: int | None = None) -> me.BatchResult:
        m = self.m
        if isinstance(payload, (bytes, str)):
            try:
                payload = orjson.loads(payload)
            except orjson.JSONDecodeError:
                return m.apply(payload, now_ms)
        now = now_ms if now_ms is not None else int(time.time() * 1000)
        batch_id = None
        if m._batch_id:
            v = me._walk(payload, m._batch_id[1])
            batch_id = None if v is _M else str(v)
        res = me.BatchResult(self.oem, self.version, batch_id)
        records = me._walk(payload, m._records_root[1])
        if not isinstance(records, list):
            return m.apply(payload, now)
        columns = None
        if m._columns:
            columns = me._walk(payload, m._columns[1])
            if not isinstance(columns, list):
                return m.apply(payload, now)
        n = len(records)
        if n == 0:
            return res
        bad: set[int] = set()
        recs = self._materialise(records, columns, bad)
        cols, present_all = self._columns(recs, payload, bad)
        if cols is None or len(bad) > _MAX_BAD_FRACTION * n:
            return m.apply(payload, now)
        good = [i for i in range(n) if i not in bad] if bad else None
        built = self._assemble(cols, present_all, good, n, now, bad)
        if built is None or len(bad) > _MAX_BAD_FRACTION * n:
            return m.apply(payload, now)
        ok_idx, dicts = built
        if not bad:
            res.ok = dicts
            return res
        slots: list[Any] = [None] * n
        for i, d in zip(ok_idx, dicts, strict=True):
            slots[i] = d
        for i in sorted(bad):                               # exact reference behaviour for anything not clean
            try:
                slots[i] = self._ref_one(records[i], columns, payload, now)
            except me.RecordError as e:
                res.failed.append(me.RecordFailure(e.reason, e.detail, records[i]))
            except (ValueError, TypeError, OverflowError) as e:
                res.failed.append(me.RecordFailure(ERR_VALIDATION_FAILED, f"{type(e).__name__}: {e}", records[i]))
        res.ok = [d for d in slots if d is not None]
        return res

    # ------------------------------------------------------------------ internals
    def _ref_one(self, raw: Any, columns: Any, payload: Any, now: int) -> dict:
        rec = raw
        if columns is not None:
            if not isinstance(raw, list) or len(raw) != len(columns):
                raise me.RecordError(ERR_BAD_ENVELOPE, "row/columns length mismatch")
            rec = dict(zip(columns, raw, strict=True))
        return self.m._map_record(rec, payload, now)

    @staticmethod
    def _materialise(records: list, columns: Any, bad: set) -> list:
        if columns is not None:
            nc = len(columns)
            out = []
            for i, r in enumerate(records):
                if type(r) is list and len(r) == nc:
                    out.append(dict(zip(columns, r, strict=True)))
                else:
                    out.append({})
                    bad.add(i)
            return out
        if set(map(type, records)) != {dict}:
            out = []
            for i, r in enumerate(records):
                if type(r) is dict:
                    out.append(r)
                else:
                    out.append({})
                    bad.add(i)
            return out
        return records

    @staticmethod
    def _extract(recs: list, payload: Any, getter: tuple) -> list:
        batch_root, toks = getter
        n = len(recs)
        if batch_root:
            return [me._walk(payload, toks)] * n
        if not toks:
            return list(recs)
        if all(type(t) is str for t in toks):
            k0 = toks[0]
            col = [r.get(k0, _M) for r in recs]
            for k in toks[1:]:
                col = [x.get(k, _M) if type(x) is dict else _M for x in col]
            return col
        return [me._walk(r, toks) for r in recs]

    def _columns(self, recs: list, payload: Any, bad: set):
        n = len(recs)
        cols: dict[str, list] = {}
        present_all: dict[str, bool] = {}
        for rule, steps in zip(self._rules, self._steps, strict=True):
            if rule.const is not _M:
                col = [rule.const] * n
            else:
                col = self._extract(recs, payload, rule.getters[0])
                for g in rule.getters[1:]:
                    other = self._extract(recs, payload, g)
                    col = [b if (a is _M or a is None) else a for a, b in zip(col, other, strict=True)]
                if None in col:
                    col = [_M if v is None else v for v in col]
            miss = [i for i, v in enumerate(col) if v is _M] if _M in col else []
            if miss:
                if rule.default is not _M:
                    for i in miss:
                        col[i] = rule.default
                    miss = []
                elif rule.required:
                    bad.update(miss)
            if steps and (n - len(miss)) > 0:
                pp = [i for i in range(n) if col[i] is not _M] if miss else None
                vals = [col[i] for i in pp] if pp is not None else col
                failed: set[int] = set()
                for st in steps:
                    vals = st.run(vals, failed)
                if isinstance(vals, np.ndarray):
                    vals = vals.tolist()
                if pp is None:
                    col = vals
                    bad.update(failed)
                else:
                    col = [_M] * n
                    for i, v in zip(pp, vals, strict=True):
                        col[i] = v
                    bad.update(pp[j] for j in failed)
                if len(bad) > _MAX_BAD_FRACTION * n:
                    return None, None
            cols[rule.name] = col
            present_all[rule.name] = not miss or rule.required
        return cols, present_all

    def _num_check(self, vals: list, lo: float, hi: float, extra_bad: set, pos: list[int]) -> None:
        """Mark positions (record indexes) whose numeric value is not of an exact int/float type or is out of range."""
        if set(map(type, vals)) <= _NUMT:
            try:
                arr = np.array(vals, dtype=np.float64)
            except OverflowError:
                extra_bad.update(pos)
                return
            okm = (arr >= lo) & (arr <= hi)
            if not okm.all():
                extra_bad.update(pos[j] for j in np.flatnonzero(~okm).tolist())
            return
        for p, v in zip(pos, vals, strict=True):
            if type(v) not in _NUMT or not (lo <= v <= hi):
                extra_bad.add(p)

    def _assemble(self, cols: dict[str, list], present_all: dict[str, bool], good: list[int] | None, n: int,
                  now: int, bad: set):
        idx = good if good is not None else list(range(n))
        sub = {k: ([v[i] for i in idx] if good is not None else v) for k, v in cols.items()}
        pos = idx
        extra: set[int] = set()
        for name, lo, hi in (("lat", -90.0, 90.0), ("lon", -180.0, 180.0), ("ts_ms", me.MIN_TS_MS, now + me.MAX_FUTURE_SKEW_MS)):
            self._num_check(sub[name], lo, hi, extra, pos)
        for name, lo, hi in (("soc_pct", 0.0, 100.0), ("fuel_pct", 0.0, 100.0), ("speed_kmh", 0.0, 350.0)):
            col = sub.get(name)
            if col is None:
                continue
            if _M in col:
                pp = [j for j, v in enumerate(col) if v is not _M]
                self._num_check([col[j] for j in pp], lo, hi, extra, [pos[j] for j in pp])
            else:
                self._num_check(col, lo, hi, extra, pos)
        if extra:
            bad.update(extra)
            keep = [j for j, p in enumerate(pos) if p not in extra]
            pos = [pos[j] for j in keep]
            sub = {k: [v[j] for j in keep] for k, v in sub.items()}
        m_ = len(pos)
        names: list[str] = []
        lists: list[list] = []
        partial: list[str] = []
        for k, v in sub.items():
            if _M in v:
                partial.append(k)
            else:
                names.append(k)
                lists.append(v)
        if "evt" not in sub:
            names.append("evt")
            lists.append(["POSITION"] * m_)
        if "dtc" not in sub:
            names.append("dtc")
            lists.append([[] for _ in range(m_)])
        if "seq" not in sub:
            names.append("seq")
            lists.append([0] * m_)
        names.append("oem")
        lists.append([self.oem] * m_)
        dicts = [dict(zip(names, row, strict=True)) for row in zip(*lists, strict=True)] if m_ else []
        defaults = {"evt": "POSITION", "seq": 0}
        for k in partial:
            col = sub[k]
            for d, v in zip(dicts, col, strict=True):
                if v is not _M:
                    d[k] = v
            if k in defaults:
                for d in dicts:
                    d.setdefault(k, defaults[k])
            elif k == "dtc":
                for d in dicts:
                    d.setdefault("dtc", [])
        if "ignition" not in sub or "ignition" in partial:
            for d in dicts:
                if "ignition" not in d:
                    d["ignition"] = d.get("speed_kmh", 0.0) > 0.0
        return pos, dicts