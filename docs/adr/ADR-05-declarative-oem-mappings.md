# ADR-05: Declarative, data-driven OEM mapping engine instead of code adapters

Status: accepted (2026-09-28)

## Context
Every OEM encodes events differently and formats change with OTA updates. Onboarding OEM number 26 must not need a deploy or downtime.

## Decision
Mappings are YAML/JSON documents interpreted by a generic engine with an allowlisted transform set and a restricted JSONPath subset (no eval). Lifecycle: draft, approved, active. Activation publishes to a compacted topic, normalizers hot-reload, and the DLQ is replayed through the new mapping. A copilot may draft a mapping, but a human approves it.

## Alternatives considered
Rejected: per-OEM Python adapters (each new OEM is a code change and a deploy).

## Consequences
Live, zero-downtime onboarding. Engine cost per record is higher than hand-written code, addressed by a batch-vectorised fast path. Mappings cannot execute code, which is a STRIDE tampering control.
