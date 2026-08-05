# Release notes

## 1.0.4 — Responsive pause and stale-control recovery

- Cancels an in-flight PostgreSQL feature batch when Pause or Cancel is requested.
- Returns the interrupted symbol batch and date chunk to `pending`, preserving completed work.
- Reconciles stale `pause_requested` jobs to `paused` after a worker restart.
- Reconciles stale `cancel_requested` jobs to `cancelled` after a worker restart.
- Bumps the feature definition version to 1.0.4; no database migration is required.

## 1.0.3 — Writable-primary connection recovery

- Rejects Supabase transaction-pooler URLs on port 6543 for this persistent worker.
- Resets leaked `default_transaction_read_only` session state when PostgreSQL connections are opened.
- Explicitly starts workbench transactions in read-write mode.
- Detects Supabase read-replica endpoints and returns a precise configuration error.
- Adds credential-free database target and read/write diagnostics to Health and System checks.
- Removes the unnecessary `CREATE EXTENSION pgcrypto` startup statement.
- Preserves all v1.0.2 feature chunks and symbol-batch checkpoints.

No data migration or feature rebuild is required.

## 1.0.2 — Resumable symbol-batched feature generation

- Splits every feature date chunk into configurable symbol batches (default 100 symbols).
- Persists batch-level completion so retries do not rebuild successful symbol batches.
- Uses UTC timestamp bounds for PostgreSQL partition pruning instead of date expressions on `bar_ts`.
- Adds batch progress to job details.
- Automatically migrates existing databases with `ra_feature_batches`.
- Existing v1.0.1 failed jobs can be retried; completed date chunks remain intact.

## 1.0.1 — Nullable universe regex fix

- Explicitly casts optional universe regex parameters as PostgreSQL text.
