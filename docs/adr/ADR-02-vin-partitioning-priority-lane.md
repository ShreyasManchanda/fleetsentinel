# ADR-02: VIN-keyed partitioning (64 partitions) with a separate priority lane

Status: accepted (2026-09-28)

## Context
Per-vehicle ordering is required by every stateful consumer (dedup, trips, going-dark), and a 30k-vehicle tenant must not create a hot partition. Critical alerts must stay under 5 s even when the bulk lane is backlogged.

## Decision
Key normalized telemetry by VIN using Kafka's murmur2 (byte-compatible implementation in `fscore.hashing`), with 64 partitions provisioned up front. OEM-pushed critical events and lender-watchlist telemetry travel a separate path (`event/*` MQTT -> `ingest.priority.v1` -> `telemetry.priority.v1`) consumed by dedicated consumer groups.

## Alternatives considered
Rejected: keying by tenant or OEM (hot spots); a single lane (critical alerts would queue behind bulk backlog); adding partitions later (remaps keys and breaks ordering).

## Consequences
Even spread, proven by a `partition_skew_ratio` metric. Cross-vehicle aggregates need a fan-in step. The priority guarantee is scoped to OEM-flagged events and watchlist vehicles (see decisions.md).
