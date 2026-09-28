"""OLTP sinks: PostgreSQL COPY (psycopg, fs_worker role), CSV files (--local) and a counting null sink (--dry-run)."""
from __future__ import annotations

import io
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.csv as pacsv
import structlog

log = structlog.get_logger("fs_simulator.sinks")
DEFAULT_DSN = "postgresql://fs_worker:fs_worker_dev@localhost:5432/fleetsentinel"
SEED_TABLES_REVERSE = ["trip", "driver_assignment", "driver", "vehicle", "depot", "fleet", "subject_key", "subscription", "tenant"]


def csv_chunks(table: pa.Table, rows: int = 250_000) -> Iterator[bytes]:
    """Arrow table -> CSV bytes (no header) in bounded chunks, written by pyarrow's C++ writer (fast)."""
    opts = pacsv.WriteOptions(include_header=False)
    for i in range(0, max(table.num_rows, 1), rows):
        buf = io.BytesIO()
        pacsv.write_csv(table.slice(i, rows), buf, opts)
        yield buf.getvalue()


class NullSink:
    def __init__(self) -> None:
        self.rows: dict[str, int] = {}

    def write(self, name: str, table: pa.Table) -> None:
        self.rows[name] = self.rows.get(name, 0) + table.num_rows

    def close(self) -> None:
        pass


class CsvDirSink(NullSink):
    """--local: one CSV per table (header included). For inspection/tests; reference ids are deterministic placeholders."""

    def __init__(self, directory: Path):
        super().__init__()
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, name: str, table: pa.Table) -> None:
        path = self.dir / f"{name}.csv"
        first = name not in self.rows
        super().write(name, table)
        with open(path, "wb" if first else "ab") as fh:
            if first:
                fh.write((",".join(table.column_names) + "\n").encode())
            for chunk in csv_chunks(table):
                fh.write(chunk)


class PostgresSink(NullSink):
    def __init__(self, dsn: str = DEFAULT_DSN, reset: bool = False):
        import psycopg

        super().__init__()
        self.conn = psycopg.connect(dsn, autocommit=False)
        with self.conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM tenant")
            existing = cur.fetchone()[0]
            if existing and not reset:
                raise SystemExit(f"tenant table already has {existing} rows; pass --reset to TRUNCATE the seeded tables first")
            if existing:
                cur.execute("TRUNCATE " + ", ".join(SEED_TABLES_REVERSE) + " CASCADE")
                log.warning("seed_reset", truncated=SEED_TABLES_REVERSE)

    def refs(self):
        """Reference ids from the database (tariff_region, vehicle_model) so FKs resolve."""
        from ..constants import MODELS
        from ..oltp import RefIds

        with self.conn.cursor() as cur:
            cur.execute("SELECT code, id FROM tariff_region")
            tariff = {c: str(i) for c, i in cur.fetchall()}
            cur.execute("SELECT name, id FROM vehicle_model")
            models = {n: str(i) for n, i in cur.fetchall()}
        return RefIds(tariff=tariff, model=[models[m[1]] for m in MODELS])

    def write(self, name: str, table: pa.Table) -> None:
        super().write(name, table)
        cols = ", ".join(table.column_names)
        with self.conn.cursor() as cur, cur.copy(f"COPY {name} ({cols}) FROM STDIN (FORMAT csv)") as cp:
            for chunk in csv_chunks(table):
                cp.write(chunk)

    def close(self) -> None:
        self.conn.commit()
        self.conn.close()
