# Cost model — FleetSentinel at 100k vehicles (spec §14.3, checkable arithmetic)

All prices: indicative AWS us-east-1 list, re-verify at submission. The model is linear in
vehicles and events/s unless noted, so it scales down to the demo seed honestly.

## Inputs

```
vehicles            = 100,000
events_per_vehicle_s = 1
events_day          = 100,000 × 86,400 = 8,640,000,000  (8.64B)
bytes_per_event = {
  json_baseline : 1,000   B   (the brief's raw-JSON reference)
  protobuf      :   120   B   (measured: ~100–130 B)
  on_broker     :    35   B   (protobuf + zstd batch compression)
  parquet_zstd  :    25   B   (columnar + zstd)
}
rollup_row      =    30   B   (1-min rollup: 144M rows/day)
```

## Daily volumes

```
lake_full_day   = 8.64B × 25 B   = 216 GB/day
broker_day      = 8.64B × 35 B   = 302 GB/day (× RF3 = 907 GB/day stored, 7-day retention)
rollups_day     = 144M × 30 B    = 4.3 GB/day
json_baseline   = 8.64B × 1000 B = 8.64 TB/day   <- what naive JSON ingestion would store
```

## Monthly steady state (100k vehicles)

| Tier | Store | Retention | Size | $/mo |
|---|---|---|---|---|
| Hot | Timescale compressed + Redis | 7 d | ~30 GB | ~$5 (gp3, compute excluded) |
| Log | Redpanda, RF=3 | 7 d | ~6.3 TB | ~$500 (gp3) |
| Warm | S3 Standard-IA, full-fidelity Parquet | 13 mo | ~85 TB | ~$1,070 |
| Cold (designed) | Glacier Deep Archive, **1-min rollups** | 7 y | ~11 TB | ~$11 |
| Cold (naive full-fidelity) | Glacier Deep Archive | 7 y | ~552 TB | ~$546 |

**Headline: ~$1,600/mo ≈ $0.016/vehicle/mo.** Two design decisions carry the savings:
1. Protobuf + columnar cuts the warm footprint ~40× vs raw JSON (216 GB/d vs 8.6 TB/d).
2. Downsampling to rollups before cold storage cuts cold cost ~50× ($11 vs $546).

## Demo seed (lite profile) — same math, small numbers

```
100k vehicles × 7 days × 1 event/6 min = ~100M rows × 25 B ≈ 2.5 GB lake
trips OLTP: ~9M rows  |  alerts: ~2M rows
```

## Live demo VM (spec §17.3)

One EC2 (or equivalent) + k3s single node, 500 GB gp3, S3 + RDS-less (self-hosted PG):
**~$15–25 for the judging window**, then destroyed (`terraform destroy`). Budget alarm at $30.
