# Release notes

## 2.0.0 — Staged discovery-engine rebuild

- Replaces monolithic discovery queries with a staged, resumable engine.
- Materialises narrow sampled rows once per period at the smallest required cadence, storing all requested outcomes on each row to avoid horizon-level duplication.
- Splits discovery by date and deterministic symbol bucket.
- Persists mergeable partial statistics for every rule group.
- Replaces full-period exact percentile sorts with a documented 0.1-percentage-point mergeable histogram.
- Retains exact means, variance components, win counts, profit-factor components, worst return and symbol/date concentration counts.
- Automatically splits timed-out chunks down to one day and one virtual symbol bucket.
- Adds transient retries for deadlocks, serialization failures, lock timeouts and dropped pooled connections.
- Adds independent statement and wall-clock limits for sample, scan and sealed chunks.
- Preserves completed feature sets and resets only withdrawn 1.x discovery artefacts on retry.
- Adds targeted schema migration `2.0.0` for the new sample, partial and sealed tables.
- Updates dashboard controls for date chunk size and initial symbol shards.
- Adds generated-query, static binding, raw-write, migration and real PostgreSQL workflow tests.

### Compatibility

No loader or feature rebuild is required. Existing candidate results from the withdrawn 1.x discovery engines must be rediscovered before sealed evaluation.

## 1.1.0 — Withdrawn monolithic engine

Version 1.1.0 corrected SQL binding and methodological-consistency defects, but its whole-period grouped query architecture was not suitable for the production-sized one-minute dataset. Its candidate results are treated as legacy by 2.0.0.
