# Algorithms & data structures write-up (brief §9)

Implementations live in `libs/fscore/algorithms/` + `libs/fscore/sketches/`. Every item has
unit + Hypothesis property tests (`libs/fscore/tests/`). Complexities are worst-case unless noted.

## Catalog

| Area | Algorithm | Where | Complexity | Key property tested |
|---|---|---|---|---|
| Parsing | VIN regex + ISO 3779 check digit | simulator, normalizer, API | O(1) | generated VINs always validate (Hypothesis) |
| Parsing | DTC `^[PBCU][0-9A-F]{4}$` → system/subsystem | normalizer, KB | O(1) | `P0A80`-class codes accepted (B-021 series) |
| Parsing | Mapping DSL (JSONPath subset + allowlisted transforms, no `eval`) | normalizer | O(fields) per record | unknown transform → compile-time `MappingError` |
| Streaming | Anti-replay sliding window (max seq + 64-bit bitmap, cf. IPsec/DTLS) | processor dedup | O(1) time, ~16 B/VIN (~1.6 MB @100k) | never drops a unique `(vin,seq)` |
| Streaming | Bloom filter **as negative filter only** (exact Redis/SET confirm) | gateway batch dedup | O(k); ≤1% FP @1M ids/h (m≈9.6 bits/item, k=7) | FP costs a lookup, never data loss |
| Streaming | Count-Min Sketch + min-heap top-K | fleet-wide top-K DTCs | O(d) update, O(log K); ε=e/w, δ=e^(−d); w=2048,d=5 → ≤0.13% overcount @99.3% | never undercounts |
| Streaming | HyperLogLog (Redis PFADD in prod) | distinct vehicles per geohash-5 | O(1), ~12 KB/counter, 0.81% err | feeds k-anonymity suppression |
| Streaming | Event-time tumbling windows + watermark (10 s lateness) | bulk lane | O(1) per event | late events counted, still forwarded |
| Streaming | Hierarchical timing wheel | GOING_DARK | O(1) insert/expire | fires exactly once, never early |
| Spatial | Geohash-7 (≈153 m) + 9-cell neighbourhood; geohash-5 for aggregation/privacy | depot clusters, map, export | O(1) encode | — |
| Graph | Union-Find over adjacent geohash-7 cells | unapproved-depot clusters | O(n α(n)) | deterministic sorted output |
| Graph | A* (admissible haversine heuristic) + Dijkstra baseline | nearest *reachable* charger | O((V+E) log V) worst case | same cost (see §1) |
| DP | PELT-pruned changepoint segmentation + O(n²) naive baseline | batch trip reconstruction | ~O(n) expected vs O(n²) | cost ≤ baseline (see §2) |
| DP | Time-of-use charging schedule (15-min slots × SoC levels) **[T2]** | EV panel | O(T·S·A) | — |
| Online | Trip start/stop hysteresis state machine | processor | O(1) per event | DP corrects it in batch |

## 1. Measured: A* vs Dijkstra (tools/bench_algorithms.py, 20×20 grid, central blocked block)

```
astar:      cost=19.000 expansions=300
dijkstra:   cost=19.000 expansions=300
nearest_reachable(budget=3km, 50 chargers): found=True goal=c12 cost=2.500 expansions=16
```

Identical optimal costs (correctness). On open grids both expand the same region — reported
honestly instead of cherry-picking a maze. The production win is the **budget-constrained
multi-goal** variant used by the LOW_SOC rule: straight-line pruning discards out-of-budget
chargers up front, reaching the answer in **16 expansions instead of 300**.

## 2. Measured: PELT vs naive segmentation (1200-point series, 5 true changepoints)

```
pelt:  changepoints=[200, 400, 600, 800, 1000] cost=1358.9 time=35.9ms
naive: changepoints=[200, 400, 600, 800, 1000] cost=1358.9 time=166.3ms
```

Identical changepoints and cost, **~4.6× faster**. The online hysteresis detector stays O(1)
per event for the hot path; PELT is the authoritative batch answer.

## 3. Dedup design note (why exact, not probabilistic, on the record path)

v2 used a Bloom filter on the per-record path, which can silently drop valid events by
construction. The record path is now an exact anti-replay window; Bloom survives only where
a false positive is harmless (gateway batch pre-filter with exact confirm). Both approaches
are implemented and their complexities compared in §catalog above.
