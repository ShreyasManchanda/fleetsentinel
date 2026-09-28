# ADR-04: Crypto-shredding for right-to-erasure in an event-sourced system

Status: accepted (2026-09-28)

## Context
The platform is event-sourced (projections rebuild by replaying an immutable log/lake) yet GDPR and DPDP require erasure. Rewriting Kafka or lake history is infeasible, and tombstoning alone lets a replay resurrect PII.

## Decision
All driver PII and driver links are AES-256-GCM encrypted under a per-subject data key (DEK). DEKs are stored only wrapped by a Vault Transit KEK (`subject_key`). Erasure destroys the wrapped DEK, nulls Postgres/Mongo/Redis copies and pseudonymizes trips. Every ciphertext in Kafka, the lake and backups becomes unreadable, and a replay renders `[erased]`.

## Alternatives considered
Rejected: deleting from immutable logs; tombstone-only erasure; one Transit key per subject (seeding cost, see decisions.md).

## Consequences
Erasure is provable (the compliance test replays and asserts unreadability) and immutability is preserved. Cost: key management and a DEK unwrap on PII read paths (cached in-process).
