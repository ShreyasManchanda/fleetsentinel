# ADR-03: Parquet lake + DuckDB for batch, Timescale continuous aggregates for hot rollups, no Spark

Status: accepted (2026-09-28)

## Context
The brief requires real-time detection plus batch analytics over billions of rows. A Spark/Hadoop cluster is operationally disproportionate for a 3-day build.

## Decision
Full-fidelity history is written as offset-named Parquet objects to S3/MinIO (Hive partitions date/oem/vehicle_shard). DuckDB queries it directly for features, reports and recompute. A Timescale hypertable plus a continuous aggregate serve the hot rollups.

## Alternatives considered
Rejected: Spark/Flink (cluster operations dominate); ClickHouse (a third analytical database to run and justify).

## Consequences
Same SQL-over-Parquet surface that Spark or Athena would expose, but this is NOT a config-change swap: a real Spark migration still needs a table format (for example Iceberg) for catalog, schema evolution and time travel. The write-up says so plainly.
