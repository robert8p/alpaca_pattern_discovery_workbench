# Release notes

## 2.3.0 — Robustness Engine v2

- Replaces whole-date robustness queries with persistent variant × date × symbol-bucket chunks.
- Reuses staged Discovery samples for development-period robustness.
- Automatically splits timed-out symbol buckets and retries transient database errors.
- Persists every robustness variant observation so retries/redeployments resume incomplete work only.
- Adds robustness chunk visibility in Job details.
- Adds targeted schema migration `2.3.0`; no raw data, feature-set or candidate rebuild is required.


## 2.2.0 — Research Integrity + Discovery Coverage Pack 1

- Preserves the six existing Discovery family definitions and completed historical candidates.
- Adds versioned partial implementations for H01, H03, H04/H05, H06, H07 and H12 using features already present in the database.
- Adds campaign name, hypothesis provenance and tested-variant and defined-grid accounting.
- Applies multiple-testing adjustment against the exact number of observed statistical tests while separately retaining the full defined grid.
- Adds p25/p75/p95/best-observation fields for new Discovery candidates.
- Adds a button-driven Robustness Lab for existing and new candidates.
- Adds date-clustered statistics, leave-one-date-out stability, cost/delay sensitivity, threshold neighbourhoods, price/liquidity breakdowns, holding-horizon MFE/MAE, missing-outcome rate, and detailed date/month/year/symbol results.
- Adds compatible cross-feature-set historical holdouts and sealed replay, restricted to the same frozen universe, liquidity tiers and predictor/baseline definition.
- Extends the Candidate analysis export with Robustness Lab summaries and detailed results.
- Adds targeted schema migration `2.2.0`; no raw data or completed feature-set rebuild is required.

## 2.1.1 — Browser-native candidate export download

- Replaces the asynchronous `fetch()` → `Blob` → synthetic anchor-click download with a normal browser GET form.
- Preserves the Candidate run and workflow-status filters as query parameters.
- Lets the browser handle the server's `Content-Disposition: attachment` response directly.
- Adds regression tests that prohibit reintroduction of the synthetic Blob-download path.
- No database schema change, feature rebuild, or discovery rerun is required.

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
