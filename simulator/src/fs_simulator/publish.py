"""CLI: python -m fs_simulator.publish [--dry-run|--bench] ... : run the 4 OEM connectors against the MQTT broker."""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from .behaviour import SimConfig
from .fleet import generate_fleet
from .publisher import DriftControl, NullTransport, SimPublisher
from .routes import build_library


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fs_simulator.publish")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--vehicles", type=int, default=None)
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=8883)
    p.add_argument("--certs", default=None)
    p.add_argument("--duration", type=float, default=60.0, help="simulated seconds to run")
    p.add_argument("--dt", type=float, default=1.0)
    p.add_argument("--start-local-hour", type=float, default=7.9, help="IST hour of day to start at (7.9 = just before the 3x burst)")
    p.add_argument("--driving-period", type=float, default=1.0)
    p.add_argument("--idle-period", type=float, default=5.0)
    p.add_argument("--parked-period", type=float, default=30.0)
    p.add_argument("--control-file", default="./data/sim_control.json", help='write {"oem_d_drift": true} here to trigger the OEM_D drift')
    p.add_argument("--d-mode", choices=["withhold", "legacy"], default="withhold")
    p.add_argument("--gt-dir", default="./data/ground_truth/live")
    p.add_argument("--dry-run", action="store_true", help="no broker: publish to a counting null transport")
    p.add_argument("--bench", action="store_true", help="dry-run, unpaced: measure events/s")
    p.add_argument("--force-drift", action="store_true")
    return p


async def amain(a: argparse.Namespace) -> dict:
    fleet, routes = generate_fleet(a.seed, a.vehicles), build_library(a.seed)
    cfg = SimConfig(emit_driving_s=a.driving_period, emit_idle_s=a.idle_period, emit_parked_s=a.parked_period,
                    emit_charging_s=a.parked_period, plan_days=max(a.duration / 86400.0, 1.0))
    drift = DriftControl(Path(a.control_file))
    if a.force_drift:
        drift.force(True)
    day0 = int(time.time() // 86400) * 86400.0 - 19800.0
    t0 = day0 + a.start_local_hour * 3600.0
    dry = a.dry_run or a.bench
    if dry:
        transport = NullTransport()
        pub = SimPublisher(fleet, routes, transport, a.seed, t0, cfg, drift, a.d_mode)
        w0 = time.perf_counter()
        stats = await pub.run(a.duration, a.dt, realtime=not a.bench)
        el = time.perf_counter() - w0
        out = {"events": stats.events, "messages": stats.messages, "wall_s": round(el, 2), "events_per_s": int(stats.events / el)}
    else:
        from .adapters.mqtt import MqttTransport

        async with MqttTransport(a.host, a.port, a.certs) as transport:
            pub = SimPublisher(fleet, routes, transport, a.seed, t0, cfg, drift, a.d_mode)
            stats = await pub.run(a.duration, a.dt, realtime=True)
        out = {"events": stats.events, "messages": stats.messages}
    gt_dir = Path(a.gt_dir)
    out["ground_truth_rows"] = pub.gt.write(gt_dir / "incidents.parquet")
    pub.ledger.write(gt_dir / "sent_ledger.parquet", fleet.oem_idx.astype(str))
    out["noise"] = pub.noise.stats.__dict__ if pub.noise else {}
    return out


def main(argv: list[str] | None = None) -> int:
    from .seed import configure_logging

    configure_logging()
    print(json.dumps(asyncio.run(amain(build_parser().parse_args(argv))), indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
