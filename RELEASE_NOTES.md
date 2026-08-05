# Release notes

## 1.0.6 — Deadlock-safe startup and feature writes

- Stops rerunning the full table/index/trigger schema on every web and worker startup once the installed schema is compatible.
- Records the installed schema version in `ra_schema_versions`.
- Creates feature partitions once per date chunk instead of once per symbol batch.
- Serializes writes to the same feature set with a PostgreSQL advisory transaction lock.
- Automatically retries deadlocks, lock timeouts and serialization conflicts with jittered backoff.
- Preserves completed chunks and batches when retrying the existing failed feature job.

No feature-table rebuild is required.

## 1.0.5 — Hard feature-batch watchdog

- Adds an independent 11-minute wall-clock watchdog for every feature SQL batch.
- Keeps job and feature-batch heartbeats fresh while PostgreSQL is working.
- Cancels and automatically splits a batch when the wall-clock deadline is exceeded.
- Force-terminates only the stuck worker backend if ordinary cancellation does not return within 15 seconds.
- Allows timed-out batches to split down to one symbol when necessary.
- Reduces the server-side statement timeout to 10 minutes and adds a 60-second lock timeout.
- Makes stale attempt 3 recoverable after a worker restart.
- Resuming a paused job now resets its claim-attempt counter safely.
- Completed date chunks and completed symbol batches remain unchanged.

No database migration is required.

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
