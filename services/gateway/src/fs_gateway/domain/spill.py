"""Bounded on-disk spill buffer for when Kafka is unreachable. Append-only segment files, CRC-framed, replayed in order.

Frame: ``u32 len(meta) | u32 len(value) | u32 crc32(meta+value) | meta(json) | value``. A torn tail (crash mid-write) is
detected by the CRC and truncated on open. ``append`` raises SpillFull rather than dropping data: the caller then stops
acknowledging MQTT, which pushes back on the OEM connector.
"""
from __future__ import annotations

import base64
import os
import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import orjson

_HDR = struct.Struct(">III")


class SpillFull(Exception):
    pass


@dataclass(slots=True)
class SpillRecord:
    topic: str
    key: bytes
    value: bytes
    headers: dict[str, bytes] = field(default_factory=dict)


def encode(rec: SpillRecord) -> bytes:
    meta = orjson.dumps({"t": rec.topic, "k": base64.b64encode(rec.key).decode(),
                         "h": {k: base64.b64encode(v).decode() for k, v in rec.headers.items()}})
    return _HDR.pack(len(meta), len(rec.value), zlib.crc32(meta + rec.value)) + meta + rec.value


def _decode(meta: bytes, value: bytes) -> SpillRecord:
    m = orjson.loads(meta)
    return SpillRecord(m["t"], base64.b64decode(m["k"]), value, {k: base64.b64decode(v) for k, v in m["h"].items()})


Cursor = tuple[int, int]     # (segment index, offset just AFTER the record)


class SpillBuffer:
    def __init__(self, directory: Path | str, max_bytes: int = 512 * 1024 * 1024, segment_bytes: int = 16 * 1024 * 1024) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.segment_bytes = max_bytes, segment_bytes
        self._cursor_file = self.dir / "cursor.json"
        self._segs: list[int] = sorted(int(p.stem.split("-")[1]) for p in self.dir.glob("seg-*.log"))
        self._sizes: dict[int, int] = {s: self._path(s).stat().st_size for s in self._segs}
        self._read_seg, self._read_off = self._load_cursor()
        self._count = 0
        self._wfile = None
        self._wseg = -1
        self._recover()

    # ------------------------------------------------------------------ public
    def __len__(self) -> int:
        return self._count

    @property
    def pending_bytes(self) -> int:
        consumed = self._read_off if self._read_seg in self._sizes else 0
        return sum(self._sizes.values()) - consumed

    def append(self, rec: SpillRecord) -> None:
        frame = encode(rec)
        if self.pending_bytes + len(frame) > self.max_bytes:
            raise SpillFull(f"spill buffer full ({self.pending_bytes} bytes)")
        if self._wfile is None or self._sizes[self._wseg] + len(frame) > self.segment_bytes:
            self._roll()
        self._wfile.write(frame)
        self._wfile.flush()
        self._sizes[self._wseg] += len(frame)
        self._count += 1

    def sync(self) -> None:
        if self._wfile is not None:
            os.fsync(self._wfile.fileno())

    def peek(self, n: int) -> list[tuple[Cursor, SpillRecord]]:
        """Up to ``n`` oldest un-acknowledged records, in order."""
        out: list[tuple[Cursor, SpillRecord]] = []
        for seg in [s for s in self._segs if s >= self._read_seg]:
            with open(self._path(seg), "rb") as f:
                f.seek(self._read_off if seg == self._read_seg else 0)
                while len(out) < n:
                    got = self._read_frame(f)
                    if got is None:
                        break
                    out.append(((seg, got[1]), got[0]))
            if len(out) >= n:
                break
        return out

    def ack(self, cursor: Cursor, n_records: int) -> None:
        """Mark ``n_records`` records, the last of which ends at ``cursor``, as delivered."""
        seg, off = cursor
        self._count = max(0, self._count - n_records)
        self._read_seg, self._read_off = seg, off
        for s in [s for s in self._segs if s < seg]:
            self._drop(s)
        if seg in self._sizes and off >= self._sizes[seg] and seg != self._wseg:
            self._drop(seg)
            self._read_seg, self._read_off = seg + 1, 0
        self._save_cursor()

    def close(self) -> None:
        if self._wfile is not None:
            self._wfile.close()
            self._wfile = None

    # ------------------------------------------------------------------ internals
    def _path(self, seg: int) -> Path:
        return self.dir / f"seg-{seg:08d}.log"

    def _roll(self) -> None:
        if self._wfile is not None:
            self._wfile.close()
        self._wseg = max((self._segs[-1] + 1) if self._segs else 0, self._read_seg)
        self._segs.append(self._wseg)
        self._sizes[self._wseg] = 0
        self._wfile = open(self._path(self._wseg), "ab")  # noqa: SIM115
        if self._read_seg < self._wseg and not any(s >= self._read_seg and s != self._wseg for s in self._segs):
            self._read_seg, self._read_off = self._wseg, 0

    def _drop(self, seg: int) -> None:
        if seg == self._wseg and self._wfile is not None:
            self._wfile.close()
            self._wfile = None
        self._path(seg).unlink(missing_ok=True)
        if seg in self._segs:
            self._segs.remove(seg)
        self._sizes.pop(seg, None)

    def _load_cursor(self) -> tuple[int, int]:
        if self._cursor_file.exists():
            try:
                c = orjson.loads(self._cursor_file.read_bytes())
                return int(c["seg"]), int(c["off"])
            except (orjson.JSONDecodeError, KeyError, ValueError):
                pass
        return (self._segs[0] if self._segs else 0), 0

    def _save_cursor(self) -> None:
        tmp = self._cursor_file.with_suffix(".tmp")
        tmp.write_bytes(orjson.dumps({"seg": self._read_seg, "off": self._read_off}))
        os.replace(tmp, self._cursor_file)

    #: Max bytes scanned forward to resync after a corrupt frame. Frames are ≤1 MB
    #: (gateway max payload); the bound keeps a pathological segment from stalling replay.
    _RESYNC_LIMIT = 2 * 1024 * 1024

    @staticmethod
    def _read_frame(f) -> tuple[SpillRecord, int] | None:
        hdr = f.read(_HDR.size)
        if len(hdr) < _HDR.size:
            return None
        ml, vl, crc = _HDR.unpack(hdr)
        body = f.read(ml + vl)
        if len(body) < ml + vl or zlib.crc32(body) != crc:
            return SpillBuffer._resync(f)
        try:
            return _decode(body[:ml], body[ml:]), f.tell()
        except Exception:
            return SpillBuffer._resync(f)

    @staticmethod
    def _resync(f):
        """A corrupt frame must not abandon later good frames in the same segment.

        Scan forward byte-by-byte (bounded) for the next header whose body CRC
        validates; the corrupt bytes are skipped and counted as lost. Returns None
        only at EOF / GIR limit (genuinely torn tail)."""
        scanned = 0
        while scanned < SpillBuffer._RESYNC_LIMIT:
            pos = f.tell()
            hdr = f.read(_HDR.size)
            if len(hdr) < _HDR.size:
                return None
            ml, vl, crc = _HDR.unpack(hdr)
            if 0 < ml <= 1_048_576 and 0 <= vl <= 1_048_576:
                body = f.read(ml + vl)
                if len(body) == ml + vl and zlib.crc32(body) == crc:
                    try:
                        return _decode(body[:ml], body[ml:]), f.tell()
                    except Exception:  # noqa: S110 - corrupt frame content: keep resyncing
                        pass
                f.seek(pos + 1)
            else:
                f.seek(pos + 1)
            scanned += 1
        return None

    def _recover(self) -> None:
        """Count pending records; truncate a torn tail in the last segment; drop fully consumed segments."""
        for seg in [s for s in self._segs if s < self._read_seg]:
            self._drop(seg)
        count = 0
        for seg in list(self._segs):
            good_end = 0
            with open(self._path(seg), "rb") as f:
                while True:
                    got = self._read_frame(f)
                    if got is None:
                        break
                    good_end = f.tell()
                    if seg > self._read_seg or good_end > self._read_off:
                        count += 1
            if good_end < self._sizes[seg] and seg == self._segs[-1]:
                with open(self._path(seg), "r+b") as f:
                    f.truncate(good_end)
                self._sizes[seg] = good_end
        self._count = count
        if self._segs:
            self._wseg = self._segs[-1]
            self._wfile = open(self._path(self._wseg), "ab")  # noqa: SIM115