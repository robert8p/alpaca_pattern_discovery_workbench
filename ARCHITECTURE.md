# Architecture — Pattern Discovery Workbench 2.0.0

## System boundary

```text
Rapid Discovery Loader              Pattern Discovery Workbench
──────────────────────              ───────────────────────────
rd_bars                 ──read──▶   quality / universe / features
rd_assets               ──read──▶   staged discovery
rd_jobs / rd_tasks      ──read──▶   sealed evaluation
                                      │
                                      ▼
                                ra_* analysis tables
```

The workbench does not write to loader-owned `rd_` tables.

## Services

### Web service

- FastAPI dashboard and API.
- Validates typed job configurations.
- Runs local and PostgreSQL planning preflight before accepting discovery or sealed jobs.
- Queues jobs and exposes status, events, chunks, candidates and system health.

### Worker

- Claims one background job using `FOR UPDATE SKIP LOCKED`.
- Executes quality, universe, feature, discovery or sealed workflows.
- Maintains heartbeat and supports pause, resume, cancel and stale-worker recovery.
- Uses the Supabase Primary Session pooler on port 5432.

## Discovery v2 persistence model

### `ra_discovery_runs`

One logical discovery run per job. Stores the immutable user configuration plus engine, rule-definition and statistics-method versions.

### `ra_discovery_sample_chunks`

Resumable instructions for materialising narrow rows by:

- discovery/validation period
- sample stride, chosen as the smallest requested horizon
- date range
- deterministic symbol-bucket range

### `ra_discovery_samples`

Narrow source table containing identifiers, predictors required by current families and all four supported forward-return columns on each sampled row. It is indexed by run, period, date and symbol bucket. Holding-period tasks select the relevant forward-return column and apply their own entry cadence.

### `ra_discovery_tasks`

One family × direction × holding-horizon combination.

### `ra_discovery_task_chunks`

Resumable bounded scans for one task and one period. A timeout changes the parent to `split` and creates two smaller children.

### `ra_discovery_partials`

One row per candidate group per completed task chunk. It contains mergeable sufficient statistics and exact symbol/date count maps.

### `ra_candidate_rules`

Shortlisted results after all partials are merged. Each candidate freezes:

- exact conditions
- direction and horizon
- entry sampling mode, stride and anchor
- rule-definition version
- statistics method
- discovery and validation metrics

### `ra_sealed_chunks`

Bounded partial evaluations of one promoted candidate over its untouched sealed period.

## Failure and replay semantics

- Before replaying a sample slice, rows for that exact run/period/date/bucket slice are deleted.
- Before replaying a scan chunk, partials owned by that chunk are deleted.
- Primary and unique keys prevent duplicate observations or partial groups.
- Statement timeout: 180 seconds by default.
- Independent wall-clock watchdog: 210 seconds.
- Pause/cancel is checked every two seconds and cancels the active PostgreSQL backend.
- Timeouts split by date first, then symbol bucket.
- Deadlocks, serialization failures, lock timeouts and dropped pooled connections retry with jittered backoff.
- Completed siblings remain committed.

## Migration strategy

Schema version `2.0.0` uses a targeted idempotent migration. Existing live analysis tables are not recreated and the full schema is not replayed when compatibility checks pass. A PostgreSQL advisory lock serialises startup migration.

## Performance design

The v2 engine deliberately avoids:

- `percentile_cont` over the full discovery period
- full-period `count(distinct ...)` for every candidate group
- one monolithic family query
- re-reading the wide feature table for every family

The wide feature source is read only during sample materialisation and sealed evaluation. Family scans operate on bounded ranges of the narrow sample table.
