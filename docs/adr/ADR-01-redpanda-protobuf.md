# ADR-01: Redpanda (Kafka API) with Schema Registry and Protobuf on the wire

Status: accepted (2026-09-28)

## Context
We need a durable, partitioned, replayable event log (brief section 7: Kafka recommended) that one engineer can run correctly in 3 days, with schema evolution for software-defined-vehicle format churn.

## Decision
Use Redpanda (Kafka-API compatible, single binary, built-in Schema Registry). Events are Protobuf (BACKWARD compatibility enforced by `buf breaking` and the registry). Application code uses `confluent-kafka` (librdkafka) and is portable to Apache Kafka, MSK or Confluent unchanged.

## Alternatives considered
Rejected: Apache Kafka + KRaft (more moving parts to operate for a 3-day build); JSON on the wire (about 8x larger than Protobuf, no schema-evolution guarantees).

## Consequences
About 8x less bandwidth and storage. Replication factor is a config knob (RF=1 dev, RF=3 in the `ha` profile). Redpanda's licence and dev-container mode are declared in the Tools Used section.
