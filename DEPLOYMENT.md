# Deployment and upgrade guide — v2.0.1

## Before deployment

1. Extract `alpaca_pattern_discovery_workbench_v2.0.1.zip` into the existing private GitHub repository.
2. Preserve hidden files, especially `.github/workflows/ci.yml` and `.python-version`.
3. Commit and push.
4. Open **GitHub → Actions → release-gate**.
5. Do not deploy until the workflow is green.

The release gate starts PostgreSQL 16 and tests schema installation, universe generation, feature generation, SQL planning, staged discovery, candidate metadata and sealed evaluation.

## Render services

Deploy the same commit to both:

```text
alpaca-pattern-workbench-web
alpaca-pattern-workbench-worker
```

Both must use the same writable Supabase Primary Session-pooler URL:

```text
postgresql://postgres.PROJECT_REF:PASSWORD@...pooler.supabase.com:5432/postgres?sslmode=require
```

Do not use port 6543 or a read-replica endpoint.

## Required environment variables

### Both services

```text
DATABASE_URL=<same Primary Session-pooler URL>
AUTO_MIGRATE=true
PYTHON_VERSION=3.12.7
DATABASE_STATEMENT_TIMEOUT_SECONDS=600
DISCOVERY_STATEMENT_TIMEOUT_SECONDS=180
DISCOVERY_WALL_TIMEOUT_SECONDS=210
DISCOVERY_CANCEL_GRACE_SECONDS=15
DISCOVERY_QUERY_RETRIES=3
LOG_LEVEL=INFO
```

### Web service

```text
APP_USERNAME=admin
APP_PASSWORD=<strong unique password>
```

### Worker service

```text
WORKER_POLL_SECONDS=3
WORKER_STALE_SECONDS=300
MAX_JOB_ATTEMPTS=3
FEATURE_BATCH_WALL_TIMEOUT_SECONDS=660
FEATURE_MIN_SYMBOL_BATCH_SIZE=1
FEATURE_CANCEL_GRACE_SECONDS=15
FEATURE_DB_CONFLICT_RETRIES=5
```

The updated `render.yaml` supplies all non-secret defaults.

## Migration

On first 2.0.1 startup, one service obtains a PostgreSQL advisory lock and applies `sql/migrations/2.0.0.sql`. It creates only the new `ra_` discovery objects and metadata columns.

It does not modify:

- `rd_bars`
- `rd_assets`
- existing universe symbols
- completed `ra_intraday_features`

After migration, later starts detect schema 2.0.0 and skip DDL.

## Deployment checks

After both services deploy:

1. Confirm web and worker show version `2.0.1`.
2. Open **System → Run checks**.
3. Confirm:
   - database port 5432
   - `is_replica=false`
   - transaction read-only values are `off`
   - all v2 objects exist
   - SQL preflight reports `ok=true`
4. Confirm the worker heartbeat is current.

## Resuming the existing failed discovery job

Your completed June/July feature set remains valid.

1. Open the failed **Initial interpretable rule scan**.
2. Click **Retry**.
3. Expect discovery progress to reset because the 1.x monolithic results are incompatible.
4. Do not recreate the feature set.

The v2 job will proceed through:

```text
sampling discovery/validation rows
→ bounded partial scans
→ merging partial statistics
→ candidate completion
```

Progress now counts sample and scan chunks, so the denominator will be much larger than the old `48` task count. That is expected and is what makes recovery granular.

## Recommended settings for the existing run

An existing job retains its saved configuration. For new scans, use:

```text
Entry sampling: non-overlapping
Scan date chunk: 3 days
Symbol shards: 4
```

For an unusually constrained Supabase compute tier, start with 1 day and 8 shards. The engine will also split automatically after a timeout.

## Troubleshooting

### A chunk times out

The parent chunk should become `split` and two smaller children should appear. Completed chunks remain intact. A one-day, one-bucket timeout is terminal and identifies a database-capacity or query-plan problem requiring inspection.

### Deadlock, serialization or short lock timeout

The chunk is returned to `pending` and retried with jittered backoff up to `DISCOVERY_QUERY_RETRIES` total attempts.

### Pause remains requested

Redeploy/restart the 2.0.1 worker. Startup recovery converts stale control states and returns active chunks to `pending`.

### Preflight blocks a job

Do not bypass it. Open **System**, inspect the exact missing object or PostgreSQL planning error, and confirm both services use the same 2.0.1 commit and database.
