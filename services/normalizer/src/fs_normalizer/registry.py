"""Active OEM mappings: loaded from contracts/mappings at startup and hot-reloaded from config.mappings.v1.

Config topic contract: key = OEM code, value = the mapping document as JSON (a tombstone / ``{"status": "retired"}``
retires it). Only a strictly higher ``version`` replaces an active mapping (active mappings are immutable, ADR-05).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson
import structlog
import yaml
from fscore.mapping_engine import Mapping, MappingError, _parse_path
from fscore.mapping_fast import FastMapping

log = structlog.get_logger("fs_normalizer.registry")


@dataclass(slots=True)
class Entry:
    oem: str
    version: int
    mapping: Mapping
    fast: FastMapping
    known_keys: frozenset[str]


def source_keys(doc: dict[str, Any]) -> frozenset[str]:
    """First-level source keys a mapping consumes (records are checked at top level only)."""
    keys: set[str] = set()
    for rule in doc.get("fields", {}).values():
        for path in ([rule["path"]] if "path" in rule else []) + list(rule.get("any", [])):
            batch_root, toks = _parse_path(path)
            if not batch_root and toks and isinstance(toks[0], str):
                keys.add(toks[0])
    return frozenset(keys)


class MappingRegistry:
    def __init__(self) -> None:
        self._by_oem: dict[str, Entry] = {}

    def get(self, oem: str) -> Entry | None:
        return self._by_oem.get(oem)

    def oems(self) -> list[str]:
        return sorted(self._by_oem)

    def versions(self) -> dict[str, int]:
        return {k: e.version for k, e in self._by_oem.items()}

    def register(self, doc: dict[str, Any], fast: bool = True) -> str:
        """Compile and install ``doc``. Returns loaded | unchanged | stale. Raises MappingError if invalid."""
        m = Mapping(doc)
        cur = self._by_oem.get(m.oem)
        if cur is not None and m.version <= cur.version:
            return "unchanged" if m.version == cur.version else "stale"
        self._by_oem[m.oem] = Entry(m.oem, m.version, m, FastMapping(m), source_keys(doc))
        return "loaded"

    def retire(self, oem: str) -> bool:
        return self._by_oem.pop(oem, None) is not None

    def load_dir(self, directory: Path, names: str | list[str]) -> list[str]:
        files = [n.strip() for n in names.split(",") if n.strip()] if isinstance(names, str) else names
        loaded = []
        for n in files:
            doc = yaml.safe_load((Path(directory) / n).read_text(encoding="utf-8"))
            if self.register(doc) == "loaded":
                loaded.append(f"{doc['oem']}@v{doc['version']}")
        return loaded

    def apply_config_message(self, key: bytes | None, value: bytes | None) -> tuple[str, str]:
        """Process one config.mappings.v1 record. Returns (outcome, oem) with outcome in
        loaded | unchanged | stale | retired | rejected."""
        oem = key.decode() if key else ""
        if value is None or not value.strip():
            return ("retired" if self.retire(oem) else "unchanged"), oem
        try:
            doc = orjson.loads(value)
            if not isinstance(doc, dict):
                raise MappingError("document is not an object")
            oem = str(doc.get("oem", oem))
            if str(doc.get("status", "")).lower() == "retired":
                return ("retired" if self.retire(oem) else "unchanged"), oem
            return self.register(doc), oem
        except (MappingError, orjson.JSONDecodeError, KeyError, TypeError, ValueError, re.error) as e:
            log.warning("mapping_rejected", oem=oem, error=str(e)[:200])
            return "rejected", oem