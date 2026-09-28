"""Seed orchestration (testable): plan, OLTP tables, lake blocks, trips, ground truth + maintenance labels."""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import structlog

from . import groundtruth as G
from .adapters.sinks import CsvDirSink, NullSink, PostgresSink
from .blocks import BlockResult, Timeline, block_indices, simulate_block
from .crypto import make_wrapper
from .features import DAY_MS, maintenance_labels
from .fleet import Fleet, generate_fleet
from .lake import VEHICLE_SHARDS, LakeWriter, make_filesystem
from .oltp import TABLE_ORDER, RefIds, build_tables, driver_ids, trips_to_table, vehicle_ids
from .routes import build_library

log = structlog.get_logger("fs_simulator.seed")
PROFILES = {"lite": {"dt_lake": 600.0}, "full": {"dt_lake": 60.0}}


@dataclass
class SeedOptions:
    profile: str = "lite"
    seed: int = 42
    vehicles: int | None = None
    anchor: str = "2026-09-28"
    trip_days: int = 30
    lake_days: int = 7
    horizon_days: int = 7
    dt_warm: float = 900.0
    local: bool = False
    dry_run: bool = False
    data_dir: str = "./data"
    dsn: str = "postgresql://fs_worker:fs_worker_dev@localhost:5432/fleetsentinel"
    minio_endpoint: str = "localhost:9000"
    bucket: str = "fleetsentinel-lake"
    skip_oltp: bool = False
    skip_lake: bool = False
    reset: bool = False
    workers: int = 1
    max_blocks: int | None = None

    def timeline(self) -> Timeline:
        return Timeline.build(self.anchor, self.trip_days, self.lake_days, self.horizon_days, self.dt_warm,
                              PROFILES[self.profile]["dt_lake"])


def plan(fleet: Fleet, tl: Timeline) -> dict:
    ticks_lake = (tl.lake_end_s - tl.lake_start_s) / tl.dt_lake
    return {"vehicles": fleet.n, "lake_rows_expected": int(fleet.n * ticks_lake), "lake_interval_s": tl.dt_lake,
            "lake_days": (tl.lake_end_s - tl.lake_start_s) / 86400, "trip_days": (tl.lake_end_s - tl.start_s) / 86400,
            "simulated_days_total": tl.total_days, "fleet": fleet.summary()}


_CACHE: dict = {}


def _worker(args: tuple) -> dict:
    """Simulate one vehicle block, write its lake files and stage its trips; returns small picklable results."""
    opts, block_id = args
    key = (opts.seed, opts.vehicles)
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = (generate_fleet(opts.seed, opts.vehicles), build_library(opts.seed))
    fleet, routes = _CACHE[key]
    tl = opts.timeline()
    gidx = block_indices(fleet)[block_id]
    lake = None
    if not opts.skip_lake and len(gidx):
        fs = make_filesystem(str(Path(opts.data_dir)) if opts.local else None, opts.minio_endpoint)
        base = str((Path(opts.data_dir) / "lake").resolve()).replace("\\", "/") if opts.local else opts.bucket
        lake = LakeWriter(fs, base, block_id)
    res: BlockResult = simulate_block(fleet, routes, gidx, opts.seed, block_id, tl, lake, shard=block_id)
    staged = None
    if res.trips:
        stage = Path(opts.data_dir) / "staging"
        stage.mkdir(parents=True, exist_ok=True)
        staged = str(stage / f"trips-{block_id:02d}.parquet")
        pq.write_table(trips_to_table(fleet, res.trips, vehicle_ids(fleet), driver_ids(fleet)), staged, compression="zstd")
    return {"block": block_id, "rows": res.rows, "ticks": res.ticks, "seconds": res.seconds, "trips_file": staged,
            "trips": int(sum(len(t["gidx"]) for t in res.trips)), "gt": res.gt.table(),
            "files": lake.files if lake else 0}


def _write_ground_truth(out: Path, fleet: Fleet, gt_tables: list[pa.Table], tl: Timeline) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    gt = pa.concat_tables(gt_tables) if gt_tables else G.GT_SCHEMA.empty_table()
    pq.write_table(gt, out / "incidents.parquet", compression="zstd")
    kind_list = gt["kind"].to_pylist()
    kinds = {k: kind_list.count(k) for k in sorted(set(kind_list))}
    bd = gt.filter(pa.array([k == "BREAKDOWN" for k in kind_list], pa.bool_()))
    index = {v: i for i, v in enumerate(fleet.vin.tolist())}
    bv = np.array([index[v] for v in bd["vin"].to_pylist()], dtype=np.int64)
    bt = bd["start_ts_ms"].to_numpy()
    preds = [int(tl.lake_end_s * 1000) - k * DAY_MS for k in range(0, 5)]
    preds = sorted(p for p in preds if p >= int(tl.lake_start_s * 1000))
    labels = maintenance_labels(fleet.vin, bv, bt, preds, tl.horizon_days_f)
    pq.write_table(labels, out / "maintenance_labels.parquet", compression="zstd")
    pos = int(np.sum(labels["will_fail"].to_numpy(zero_copy_only=False)))
    return {"incident_rows": gt.num_rows, "incidents_by_kind": kinds, "label_rows": labels.num_rows, "label_positives": pos,
            "label_prevalence": round(pos / max(labels.num_rows, 1), 5)}


def run_seed(opts: SeedOptions) -> dict:
    t_all = time.perf_counter()
    fleet = generate_fleet(opts.seed, opts.vehicles)
    routes = build_library(opts.seed)
    tl = opts.timeline()
    report: dict = {"profile": opts.profile, "mode": "dry-run" if opts.dry_run else ("local" if opts.local else "postgres+minio"),
                    "plan": plan(fleet, tl)}
    data = Path(opts.data_dir)
    wrapper = make_wrapper(local=opts.local or opts.dry_run)
    report["kek"] = wrapper.label
    refs = RefIds()
    if opts.dry_run or opts.skip_oltp:
        sink: NullSink = NullSink()
    elif opts.local:
        sink = CsvDirSink(data / "oltp")
    else:
        sink = PostgresSink(opts.dsn, opts.reset)
        refs = sink.refs()
    t = time.perf_counter()
    tables, _ = build_tables(fleet, routes, wrapper, opts.seed, refs)
    report["oltp_build_s"] = round(time.perf_counter() - t, 2)
    t = time.perf_counter()
    for name in TABLE_ORDER[:-1]:
        sink.write(name, tables[name])
    report["oltp_write_s"] = round(time.perf_counter() - t, 2)
    _CACHE.clear()
    _CACHE[(opts.seed, opts.vehicles)] = (fleet, routes)
    if opts.dry_run:                       # time ONE block for one simulated day and extrapolate
        probe = SeedOptions(**{**asdict(opts), "trip_days": 1, "lake_days": 1, "horizon_days": 0, "skip_lake": True})
        r = _worker((probe, 0))
        per_vehicle_tick = r["seconds"] / max(len(block_indices(fleet)[0]) * r["ticks"], 1)
        report["dry_run_estimate_sim_s"] = round(per_vehicle_tick * fleet.n * (tl.end_s - tl.start_s) / tl.dt_warm, 1)
        report["oltp_rows"] = sink.rows
        report["total_s"] = round(time.perf_counter() - t_all, 2)
        return report
    n_blocks = VEHICLE_SHARDS if opts.max_blocks is None else opts.max_blocks
    t = time.perf_counter()
    jobs = [(opts, b) for b in range(n_blocks)]
    results = []
    if opts.workers > 1:
        from concurrent.futures import ProcessPoolExecutor

        with ProcessPoolExecutor(max_workers=opts.workers) as pool:
            results = list(pool.map(_worker, jobs))
    else:
        for j in jobs:
            results.append(_worker(j))
            log.info("block_done", **{k: v for k, v in results[-1].items() if k not in ("gt", "trips_file")})
    report["sim_s"] = round(time.perf_counter() - t, 2)
    report["lake_rows"] = int(sum(r["rows"] for r in results))
    report["lake_files"] = int(sum(r["files"] for r in results))
    report["lake_rows_per_s"] = int(report["lake_rows"] / max(report["sim_s"], 1e-9))
    t = time.perf_counter()
    n_trips = 0
    for r in results:
        if r["trips_file"]:
            tb = pq.read_table(r["trips_file"])
            sink.write("trip", tb)
            n_trips += tb.num_rows
    report["trips"], report["trip_write_s"] = n_trips, round(time.perf_counter() - t, 2)
    report["ground_truth"] = _write_ground_truth(data / "ground_truth", fleet, [r["gt"] for r in results], tl)
    report["oltp_rows"] = sink.rows
    sink.close()
    report["total_s"] = round(time.perf_counter() - t_all, 2)
    data.mkdir(parents=True, exist_ok=True)
    (data / "seed_report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    log.info("seed_done", **{k: v for k, v in report.items() if k in ("mode", "lake_rows", "trips", "total_s")})
    return report
