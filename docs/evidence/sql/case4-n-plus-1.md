# Case 4: vehicle list with alert counts — ORM N+1 vs single join

200 vehicles (tenant `bb312e09…`).

## BEFORE (1 + N lazy queries): 201 queries, 1091 ms

The deliberately naive ORM version lazy-loads one COUNT per vehicle.

## AFTER (single grouped join): 1 query, 36 ms

One `LEFT JOIN + GROUP BY` (or `selectinload`) returns all counts at once.
