# Release notes

## 2.1.0 — Candidate analysis export

- Adds **Download analysis export** to the Candidate tab.
- Export respects current discovery-run and workflow-status filters.
- Produces a single ZIP designed to upload back into ChatGPT for analysis.
- Includes `SUMMARY.md`, full candidate CSV/JSON, discovery configuration/tasks, feature-set provenance and relevant frozen universe symbols.
- Embeds discovery/validation dates, round-trip cost and screening thresholds directly in the flattened candidate CSV.
- Includes a ready-made `ANALYSIS_PROMPT.txt`.
- Uses only read queries against existing analysis tables; no schema migration or discovery rerun is required.

## 2.0.1 — PostgreSQL migration-startup correction

- Corrects the schema-version UPSERT to use the primary-key conflict target: `ON CONFLICT (version) DO UPDATE`.
- Adds a repository-wide static release check that rejects `ON CONFLICT DO UPDATE` without an inference target or constraint name.
- Adds regression tests for the schema-version UPSERT and all `ON CONFLICT ... DO UPDATE` statements.
- Keeps the staged v2 discovery methodology, schema version and rule-definition version unchanged.
- No raw `rd_` data or completed feature set needs to be rebuilt.

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
