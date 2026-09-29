# Case 3: trip history deep page — OFFSET vs keyset

Page at depth 100,000 (50 rows). Keyset cursor = `(start_ts, id)`.

## BEFORE (OFFSET scans and discards 100k rows)

```
Limit  (cost=4693.35..4695.70 rows=50 width=24) (actual time=25.573..25.582 rows=50 loops=1)
  Buffers: shared hit=27887
  ->  Index Only Scan using trip_tenant_keyset on trip  (cost=0.42..5631.94 rows=120000 width=24) (actual time=0.062..22.416 rows=100050 loops=1)
        Index Cond: (tenant_id = 'bb312e09-003a-4f78-acbb-1eea4d2ac3ac'::uuid)
        Heap Fetches: 26991
        Buffers: shared hit=27887
Planning:
  Buffers: shared hit=4
Planning Time: 0.204 ms
Execution Time: 25.601 ms
```

## AFTER (keyset seeks directly)

```
Limit  (cost=0.42..5.11 rows=50 width=24) (actual time=0.019..0.028 rows=50 loops=1)
  Buffers: shared hit=7
  ->  Index Only Scan using trip_tenant_keyset on trip  (cost=0.42..1854.96 rows=19755 width=24) (actual time=0.017..0.023 rows=50 loops=1)
        Index Cond: ((tenant_id = 'bb312e09-003a-4f78-acbb-1eea4d2ac3ac'::uuid) AND (ROW(start_ts, id) < ROW('2026-09-04 00:00:00+00'::timestamp with time zone, '8815b50b-f1a2-4371-b847-12c45abeb721'::uuid)))
        Heap Fetches: 3
        Buffers: shared hit=7
Planning:
  Buffers: shared hit=4
Planning Time: 0.188 ms
Execution Time: 0.058 ms
```
