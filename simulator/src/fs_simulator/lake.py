"""Lake writer: telemetry Parquet with Hive partitions date=/oem=/vehicle_shard= (local dir or MinIO via S3 API)."""
from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pyarrow as pa
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from .behaviour import Emission
from .constants import DTC_ALL, EVT_NAMES, OEM_CODES
from .fleet import Fleet

VEHICLE_SHARDS = 16
_EVT_DICT = pa.array(EVT_NAMES)
_DTC_DICT = pa.array(DTC_ALL)

LAKE_SCHEMA = pa.schema([
    ("vin", pa.dictionary(pa.int32(), pa.string())), ("ts_ms", pa.int64()), ("seq", pa.int64()),
    ("lat", pa.float32()), ("lon", pa.float32()), ("speed_kmh", pa.float32()), ("soc_pct", pa.float32()),
    ("fuel_pct", pa.float32()), ("soh_pct", pa.float32()), ("odo_km", pa.float32()), ("ignition", pa.bool_()),
    ("evt", pa.dictionary(pa.int8(), pa.string())), ("dtc", pa.dictionary(pa.int8(), pa.string())), ("state", pa.int8()),
])


def vehicle_shard_of(fleet: Fleet) -> np.ndarray:
    """Lake partition key: murmur2 shard (64) folded into 16 vehicle_shard buckets."""
    return (fleet.shard // (64 // VEHICLE_SHARDS)).astype(np.int16)


def _f32(a: np.ndarray) -> pa.Array:
    a = np.asarray(a, dtype=np.float32)
    return pa.array(a, mask=np.isnan(a))


def emission_to_table(em: Emission, block_vins: np.ndarray) -> pa.Table:
    """Emission -> Arrow table (vin/evt/dtc dictionary-encoded, NaN -> null). block_vins is indexed by em.idx."""
    vin = pa.DictionaryArray.from_arrays(pa.array(em.idx.astype(np.int32)), pa.array(block_vins))
    evt = pa.DictionaryArray.from_arrays(pa.array(em.evt.astype(np.int8)), _EVT_DICT)
    dtc = pa.DictionaryArray.from_arrays(pa.array(em.dtc.astype(np.int8), mask=em.dtc < 0), _DTC_DICT)
    return pa.table({
        "vin": vin, "ts_ms": pa.array(em.ts_ms), "seq": pa.array(em.seq), "lat": _f32(em.lat), "lon": _f32(em.lon),
        "speed_kmh": _f32(em.speed), "soc_pct": _f32(em.soc), "fuel_pct": _f32(em.fuel), "soh_pct": _f32(em.soh),
        "odo_km": _f32(em.odo), "ignition": pa.array(em.ign), "evt": evt, "dtc": dtc, "state": pa.array(em.state.astype(np.int8)),
    }, schema=LAKE_SCHEMA)


def make_filesystem(local_root: str | None = None, endpoint: str = "localhost:9000", access_key: str = "minioadmin",
                    secret_key: str = "minioadmin"):  # noqa: S107 — local-dev MinIO default, matches compose; prod uses env/Vault
    if local_root is not None:
        return pafs.LocalFileSystem()
    return pafs.S3FileSystem(endpoint_override=endpoint, scheme="http", access_key=access_key, secret_key=secret_key, region="us-east-1")


class LakeWriter:
    def __init__(self, fs, base: str, block_id: int, compression_level: int = 1):
        self.fs, self.base, self.block_id, self.level = fs, base.rstrip("/"), block_id, compression_level
        self.files = 0
        self.rows = 0
        self.bytes = 0
        self._part = 0

    def write(self, em: Emission, block_vins: np.ndarray, oem_of_row: np.ndarray, shard: int) -> None:
        """Split rows by (UTC date, OEM) and write one Parquet file per partition."""
        if not len(em):
            return
        day = em.ts_ms // 86_400_000
        table = emission_to_table(em, block_vins)
        for d in np.unique(day):
            dm = day == d
            date = datetime.fromtimestamp(int(d) * 86400, UTC).strftime("%Y-%m-%d")
            for oi, code in enumerate(OEM_CODES):
                m = dm & (oem_of_row == oi)
                if not m.any():
                    continue
                self._part += 1
                dirp = f"{self.base}/telemetry/date={date}/oem={code}/vehicle_shard={shard:02d}"
                self.fs.create_dir(dirp, recursive=True)
                path = f"{dirp}/part-{self.block_id:03d}-{self._part:05d}.parquet"
                sub = table.filter(pa.array(m))
                pq.write_table(sub, path, filesystem=self.fs, compression="zstd", compression_level=self.level, row_group_size=1 << 20)
                self.files += 1
                self.rows += sub.num_rows
