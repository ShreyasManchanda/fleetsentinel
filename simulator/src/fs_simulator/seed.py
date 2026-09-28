"""CLI: python -m fs_simulator.seed --profile lite|full [--local] [--dry-run] ..."""
from __future__ import annotations

import argparse
import json
import logging
import sys

import structlog

from .seed_core import PROFILES, SeedOptions, run_seed


def configure_logging() -> None:
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    structlog.configure(processors=[structlog.processors.add_log_level, structlog.processors.TimeStamper(fmt="iso"),
                                    structlog.processors.JSONRenderer()],
                        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr))


def parse(argv: list[str] | None = None) -> SeedOptions:
    p = argparse.ArgumentParser(prog="fs_simulator.seed", description="FleetSentinel seed: OLTP + lake + ground truth")
    p.add_argument("--profile", choices=sorted(PROFILES), default="lite")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--vehicles", type=int, default=None, help="override 100000 (testing only; real profiles simulate all)")
    p.add_argument("--anchor", default="2026-09-28")
    p.add_argument("--trip-days", type=int, default=30)
    p.add_argument("--lake-days", type=int, default=7)
    p.add_argument("--local", action="store_true", help="no Docker: CSV OLTP + Parquet lake under --data-dir, local dev KEK")
    p.add_argument("--dry-run", action="store_true", help="plan + build OLTP tables in memory + time one block; write nothing")
    p.add_argument("--data-dir", default="./data")
    p.add_argument("--dsn", default="postgresql://fs_worker:fs_worker_dev@localhost:5432/fleetsentinel")
    p.add_argument("--minio-endpoint", default="localhost:9000")
    p.add_argument("--skip-oltp", action="store_true")
    p.add_argument("--skip-lake", action="store_true")
    p.add_argument("--reset", action="store_true", help="TRUNCATE seeded tables first (Postgres mode)")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--max-blocks", type=int, default=None, help="simulate only the first N of 16 vehicle blocks (testing)")
    a = p.parse_args(argv)
    return SeedOptions(profile=a.profile, seed=a.seed, vehicles=a.vehicles, anchor=a.anchor, trip_days=a.trip_days,
                       lake_days=a.lake_days, local=a.local, dry_run=a.dry_run, data_dir=a.data_dir, dsn=a.dsn,
                       minio_endpoint=a.minio_endpoint, skip_oltp=a.skip_oltp, skip_lake=a.skip_lake, reset=a.reset,
                       workers=a.workers, max_blocks=a.max_blocks)


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    print(json.dumps(run_seed(parse(argv)), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
