# Case 2: fleet cost summary — full aggregation vs materialized view

Tenant `sql-ev-a`, 120,000 trip rows. Refresh took 156 ms (scheduled, off-request).

## BEFORE (aggregate ~120k trip rows per request)

```
Finalize GroupAggregate  (cost=4813.56..4911.00 rows=672 width=72) (actual time=35.727..40.162 rows=28 loops=1)
  Group Key: (date_trunc('day'::text, start_ts))
  Buffers: shared hit=2191
  ->  Gather Merge  (cost=4813.56..4890.84 rows=672 width=72) (actual time=35.717..40.126 rows=56 loops=1)
        Workers Planned: 1
        Workers Launched: 1
        Buffers: shared hit=2191
        ->  Sort  (cost=3813.55..3815.23 rows=672 width=72) (actual time=24.446..24.460 rows=28 loops=2)
              Sort Key: (date_trunc('day'::text, start_ts))
              Sort Method: quicksort  Memory: 29kB
              Buffers: shared hit=2191
              Worker 0:  Sort Method: quicksort  Memory: 29kB
              ->  Partial HashAggregate  (cost=3770.23..3781.99 rows=672 width=72) (actual time=24.342..24.353 rows=28 loops=2)
                    Group Key: date_trunc('day'::text, start_ts)
                    Batches: 1  Memory Usage: 73kB
                    Buffers: shared hit=2182
                    Worker 0:  Batches: 1  Memory Usage: 73kB
                    ->  Parallel Seq Scan on trip  (cost=0.00..3240.82 rows=70588 width=20) (actual time=0.206..14.704 rows=60000 loops=2)
                          Filter: (tenant_id = 'bb312e09-003a-4f78-acbb-1eea4d2ac3ac'::uuid)
                          Buffers: shared hit=2182
Planning:
  Buffers: shared hit=145
Planning Time: 1.098 ms
Execution Time: 40.563 ms
```

## AFTER (read pre-aggregated daily rows)

```
GroupAggregate  (cost=1.36..1.39 rows=1 width=72) (actual time=0.032..0.052 rows=28 loops=1)
  Group Key: day
  Buffers: shared hit=1
  ->  Sort  (cost=1.36..1.36 rows=1 width=72) (actual time=0.026..0.029 rows=28 loops=1)
        Sort Key: day
        Sort Method: quicksort  Memory: 26kB
        Buffers: shared hit=1
        ->  Seq Scan on mv_fleet_cost_daily  (cost=0.00..1.35 rows=1 width=72) (actual time=0.013..0.018 rows=28 loops=1)
              Filter: (tenant_id = 'bb312e09-003a-4f78-acbb-1eea4d2ac3ac'::uuid)
              Buffers: shared hit=1
Planning:
  Buffers: shared hit=18 read=1
Planning Time: 0.329 ms
Execution Time: 0.074 ms
```
