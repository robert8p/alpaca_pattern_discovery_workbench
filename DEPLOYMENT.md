# Deployment and upgrade guide — v1.1.0

This is a separate Render application and GitHub repository, but it uses the **same Supabase database** as the Alpaca Rapid Discovery Loader.

## 1. Replace the repository

1. Extract `alpaca_pattern_discovery_workbench_v1.1.0.zip`.
2. Replace the contents of the existing workbench repository.
3. Include hidden paths:
   - `.python-version`
   - `.gitignore`
   - `.env.example`
   - `.github/workflows/ci.yml`
4. Do not upload `.env` or any credentials.
5. Commit and push.

## 2. Require the GitHub release gate

Open the repository's **Actions** tab and wait for `release-gate` to pass.

It runs:

- Python compilation
- JavaScript syntax validation
- Unit and static-integrity tests
- PostgreSQL 16 synthetic end-to-end integration
- Exhaustive PostgreSQL planning of 194 generated production queries
- Release audit, raw-table write scan and version consistency checks

Do not deploy a commit whose workflow is red.

## 3. Database connection

Both Render services must use the same writable Supabase **Primary Session pooler** URL on port 5432:

```text
postgresql://postgres.PROJECT_REF:YOUR_PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

Do not use:

- Port 6543 transaction pooling
- A read replica
- A role that cannot create/update `ra_` objects

## 4. Render services

The Blueprint defines:

- `alpaca-pattern-workbench-web`
- `alpaca-pattern-workbench-worker`

Both pin:

```text
PYTHON_VERSION=3.12.7
AUTO_MIGRATE=true
```

The existing `DATABASE_URL` and `APP_PASSWORD` values have `sync: false`; Blueprint updates do not replace them. Verify them manually in Render.

## 5. Timeout settings

The Blueprint supplies:

```text
DATABASE_STATEMENT_TIMEOUT_SECONDS=600
FEATURE_BATCH_WALL_TIMEOUT_SECONDS=660
FEATURE_CANCEL_GRACE_SECONDS=15
FEATURE_MIN_SYMBOL_BATCH_SIZE=1
FEATURE_DB_CONFLICT_RETRIES=5
DISCOVERY_STATEMENT_TIMEOUT_SECONDS=600
DISCOVERY_WALL_TIMEOUT_SECONDS=660
DISCOVERY_CANCEL_GRACE_SECONDS=15
DISCOVERY_QUERY_RETRIES=2
```

Do not increase these simply to conceal a query that is failing to make progress. The watchdogs and batching exist to expose and divide oversized work.

## 6. Deploy

Deploy both services from the same commit. Confirm their logs show:

```text
Python 3.12.7
version 1.1.0
```

The schema upgrade is minimal. It adds candidate methodology metadata and records schema version 1.1.0. Existing `rd_` bars and completed feature rows are unchanged.

## 7. Mandatory deployed preflight

Open **System → Run checks**.

Require all of the following:

- Database target is port 5432
- `is_replica=false`
- Transaction read-only settings are `off`
- `rd_bars` and `rd_assets` exist
- `ra_` schema is ready
- Worker heartbeat is current
- SQL preflight reports `ok=true`
- A query-definition hash is displayed
- PostgreSQL plans are greater than zero

The API refuses new discovery and sealed-test jobs if this preflight fails.

## 8. Existing feature set

Your completed June/July regular-session feature set does **not** need rebuilding. Feature definition 1.1.0 changes release validation and metadata, not the stored predictor semantics used by the existing set.

Keep the existing feature set unless its own quality or coverage is suspect.

## 9. Existing failed discovery run

Open **Initial interpretable rule scan** and click **Retry** only after:

1. GitHub Actions is green.
2. Web and worker both show 1.1.0.
3. System SQL preflight passes.

On retry, progress will reset from the old partial result to `0/48`. This is deliberate. The workbench deletes legacy candidates and reruns every discovery task using one consistent v1.1.0 methodology.

The feature table is not rebuilt.

## 10. Acceptance sequence

For the existing data:

1. Run System checks.
2. Inspect the feature-set coverage.
3. Retry the existing discovery scan.
4. Confirm task progress advances through all 48 combinations.
5. Review candidates only after the run status is `completed`.
6. Shortlist a candidate based on net return, samples, concentration and validation—not win rate alone.
7. Trigger sealed evaluation only after the candidate definition is frozen.

## 11. Recovery behaviour

- Pause and Cancel cancel in-flight database work through an independent monitor.
- Worker restarts reconcile stale control states.
- Feature retries preserve completed chunks and symbol batches.
- Discovery retries preserve compatible completed tasks; an engine-version change resets all tasks.
- Legacy candidates cannot be sealed.
- Upstream analysis assets cannot be deleted through the dashboard while dependants exist.

## 12. Troubleshooting

### SQL preflight fails

Do not queue analysis. Open System details and inspect the exact missing object or failed PostgreSQL plan. Confirm both services are on the same 1.1.0 commit and database.

### Worker is absent

Check the worker's `DATABASE_URL`, startup migration log and heartbeat. The web service cannot execute queued analysis on its own.

### Read-only transaction

Replace `DATABASE_URL` on both services with the Supabase Primary Session pooler on port 5432 and redeploy.

### Feature timeout

The worker automatically splits symbol batches. Retry the same feature job; do not recreate it. Completed work is retained.

### Discovery timeout

The task retries once with jitter. If it still fails, inspect database compute and the selected every-bar/non-overlapping sampling mode. Non-overlapping is the recommended default.

### Old percent-placeholder failure

That defect belongs to v1.0.7 and earlier. v1.1.0 uses machine-safe category codes and validates all generated SQL through Psycopg grammar and PostgreSQL preflight before execution.
