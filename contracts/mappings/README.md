# OEM mapping DSL (frozen contract)

A mapping is **data, not code**: a YAML/JSON document interpreted by the generic engine in `fscore.mapping_engine`
(ADR-05). Onboarding an OEM = adding a mapping, never deploying an adapter. Mappings **cannot execute code**: transforms
come from a fixed allowlist, paths are a restricted JSONPath subset, there is no `eval`.

Lifecycle (`schema_mapping.status`): `draft` -> `approved` -> `active` -> `retired`. An active mapping is immutable; to
change it, create version N+1. Activation publishes the document to the compacted topic `config.mappings.v1`
(key = OEM code); every normalizer hot-reloads within ~2 s. After activation, the OEM's DLQ is replayed through it.

## Document

```yaml
oem: OEM_B                    # must match oem.code
version: 3                    # integer, unique per OEM
envelope:
  records: "$.d[*]"           # path selecting the records inside a batch payload
  batch_id: "$.bid"           # optional: connector batch id (dedup)
  columns: "$.cols"           # optional: COLUMNAR envelopes - each record is an array zipped with this column list
fields:                       # canonical field -> rule (see below)
  vin:       { path: "$.v", transform: [strip, upper, validate_vin] }
  ...
drop_unmapped: true           # data minimisation: unmapped source fields never enter the platform (default true)
```

Paths are evaluated against **one record** (root `$`). Batch-level fields (e.g. a `sw` in the envelope) are addressable with
`$$` = the whole batch payload (e.g. `$$.meta.fw`).

### Path subset
`$` root - `.key` - `['key']` - `[n]` index - `[*]` wildcard (envelope `records` only). No filters, no recursion, no
functions. Missing path => value absent (then `default`/`required` decide).

### Field rule
```yaml
<canonical_field>:
  path: "$.x"                 # or:
  any: ["$.a", "$.b"]         #   first present wins
  const: "value"              #   constant (e.g. oem code)
  transform: [ ... ]          # applied in order
  default: value              # used when absent
  required: true|false        # defaults: vin, ts_ms, lat, lon required; others optional
```

### Canonical fields
`vin`, `ts_ms`, `seq`, `lat`, `lon`, `speed_kmh`, `soc_pct`, `fuel_pct`, `odo_km`, `dtc` (list), `evt`, `ignition`,
`sw_version`, `soh_pct`, `engine_temp_c`, `driver_ref` (opaque source id; the normalizer encrypts it with Vault Transit into
`driver_ref_enc` - it is never stored or logged in plaintext).

### Transform allowlist
| Transform | Effect |
|---|---|
| `strip`, `upper`, `lower` | string cleanup |
| `to_float`, `to_int`, `to_bool` | coercion (`"1"`, `1`, `true`, `"true"` -> true) |
| `mul: n`, `add: n`, `round: n` | arithmetic (unit conversion: mph->km/h `mul: 1.609344`) |
| `clamp: [lo, hi]` | bound a number |
| `pct_from_fraction` | 0..1 -> 0..100 |
| `epoch_s_to_ms` | seconds -> milliseconds |
| `iso8601_to_ms` | ISO-8601 string -> epoch ms (UTC) |
| `split_csv` | `"P0301,P0171"` -> list |
| `enum_map: {SRC: CANON}` | map source codes to canonical `EventType` names; unmapped -> `POSITION` |
| `validate_vin` | ISO 3779 check (fscore.vin) - failure sends the record to the DLQ (`VALIDATION_FAILED`) |
| `validate_dtc` | keep only `^[PBCU][0-9A-F]{4}$` codes, de-duplicated |

Canonical `evt` values: `POSITION HARSH_BRAKE HARSH_ACCEL IGNITION_ON IGNITION_OFF TOW CRASH CHARGING_START CHARGING_END DTC_SET OVERSPEED`.

## Validation the engine performs on every record
1. required fields present; 2. `vin` valid; 3. `lat` in [-90,90], `lon` in [-180,180]; 4. `ts_ms` not more than 2 min in the
future (else `CLOCK_SKEW`) and not before 2020; 5. numeric ranges (`soc_pct`,`fuel_pct` 0..100; `speed_kmh` 0..350).
A record failing any check goes to `dlq.ingest.v1` with header `error_reason`; the rest of the batch continues.

## Files
| File | Purpose |
|---|---|
| `oem_a.yaml` | nested JSON, ISO timestamps, `lng`, km/h (active from the start) |
| `oem_b.yaml` | flat abbreviated keys, epoch ms, mph, miles, CSV DTCs, fuel % (active from the start) |
| `oem_c.yaml` | the brief's illustrative shape, near-canonical (active from the start) |
| `oem_d_draft.yaml` | **columnar** array-encoded format, epoch seconds, fractional SoC - NOT registered; used for the live-onboarding demo |
| `samples/*.json` | one realistic batch payload per OEM (also used by tests, the editor preview and the copilot's mapping draft) |
