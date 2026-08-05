# Release notes

## 1.0.2 — Resumable symbol-batched feature generation

- Splits every feature date chunk into configurable symbol batches (default 100 symbols).
- Persists batch-level completion so retries do not rebuild successful symbol batches.
- Uses UTC timestamp bounds for PostgreSQL partition pruning instead of date expressions on `bar_ts`.
- Adds batch progress to job details.
- Automatically migrates existing databases with `ra_feature_batches`.
- Existing v1.0.1 failed jobs can be retried; completed date chunks remain intact.

## 1.0.1 — Nullable universe regex fix

- Explicitly casts optional universe regex parameters as PostgreSQL text.
