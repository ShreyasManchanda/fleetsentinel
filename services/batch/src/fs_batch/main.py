"""fs_batch entry: dispatch batch jobs over lake Parquet (DuckDB, no cluster).

Usage: python -m fs_batch <features|recompute|reports|reconcile> --lake <glob> [...]
"""
from __future__ import annotations

import argparse
import json
import sys

import duckdb
import structlog

log = structlog.get_logger("fs_batch")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fs_batch")
    ap.add_argument("job", choices=["features", "recompute", "reports", "reconcile"])
    ap.add_argument("--lake", required=True, help="Parquet glob, e.g. './data/lake/**/*.parquet'")
    ap.add_argument("--out", default="-", help="output JSON path ('-' = stdout)")
    args = ap.parse_args(argv)
    con = duckdb.connect()
    if args.job == "features":
        from .jobs.features import build_day_features

        rows = build_day_features(con, args.lake)
        payload = {"rows": len(rows), "sample": [r.__dict__ for r in rows[:5]]}
    elif args.job == "reports":
        from .jobs.reports import carbon_by_fleet, fuel_idle_by_depot

        payload = {"depots": [d.__dict__ for d in fuel_idle_by_depot(con, args.lake)],
                   "carbon": [c.__dict__ for c in carbon_by_fleet(con, args.lake)]}
    elif args.job == "recompute":
        from .jobs.recompute import authoritative_hourly

        rows = authoritative_hourly(con, args.lake)
        payload = {"windows": len(rows), "sample": [r.__dict__ for r in rows[:5]]}
    else:
        return _reconcile_offline(args)
    blob = json.dumps(payload, default=str)
    if args.out == "-":
        print(blob)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(blob)
    log.info("batch_done", job=args.job, lake=args.lake)
    return 0


def _reconcile_offline(args) -> int:
    import pyarrow.parquet as pq

    from .jobs.reconcile import Incident, reconcile

    gt = pq.read_table(args.lake).to_pylist()
    incidents = [Incident(kind=r["kind"], vin=r["vin"], start_ts_ms=int(r["start_ts_ms"])) for r in gt]
    res = reconcile(incidents, [])
    print(json.dumps(res.__dict__, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
