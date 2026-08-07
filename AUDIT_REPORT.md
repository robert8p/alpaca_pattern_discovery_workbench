# Alpaca Pattern Discovery Workbench v2.3.0 — Robustness Engine v2 Audit

## Scope

This release rebuilds the Robustness Lab execution engine after a production development-period run failed at 239/258 steps with `canceling statement due to statement timeout`.

Discovery family definitions, completed feature sets, frozen universes, candidate definitions and raw `rd_` data are unchanged.

## Root cause of the v2.2.0 timeout

The v2.2.0 Robustness Lab was not using the bounded Discovery architecture. It executed one query per full trading date for each entry-delay or neighbourhood variant. Each query reconstructed signal state from `ra_intraday_features`, including joins/window calculations and holding-horizon excursion calculations. The failing run had completed:

- all configured entry-delay passes;
- the complete relaxed-neighbourhood pass; and
- part of the tightened-neighbourhood pass.

The progress value 239/258 therefore represented in-memory loop progress, not 239 durable robustness tasks. Only base observations were persisted. Non-base delay/neighbourhood results were held in memory until finalisation, so a failed v2.2.0 run could not resume from step 240.

## v2.3.0 architecture

Robustness now uses persistent, bounded work units:

`variant × trade date × deterministic symbol bucket`

New tables:

- `ra_robustness_chunks`
- `ra_robustness_samples`

Every completed chunk commits its observations independently. Parent chunks can be split into smaller symbol-bucket ranges after a timeout. Worker restart, pause and retry release only incomplete chunks back to `pending`.

### Development-mode optimisation

When robustness replays a candidate against its original completed feature set, the engine reads the already-materialised `ra_discovery_samples`. It does not reconstruct signal state from the complete intraday feature table.

Development mode therefore starts with one bucket covering the entire symbol universe per date/variant. If a date still exceeds the timeout, that date is automatically split by deterministic symbol bucket.

### Historical-holdout mode

A compatible external feature set has no pre-existing Discovery sample table for the candidate. Holdout robustness therefore reconstructs the required signal fields from `ra_intraday_features`, but each query is bounded to one date and one symbol-bucket range. Holdouts start with four symbol shards by default and can split further on timeout.

## Statistical behaviour

The Robustness Lab outputs are unchanged in interpretation. It continues to calculate:

- exact gross/net observation returns;
- date-clustered t-statistic;
- leave-one-date-out minimum expectancy;
- cost sensitivity;
- entry-delay sensitivity;
- relaxed/exact/tightened threshold-neighbourhood results;
- liquidity-tier and price-group breakdowns;
- date/month/year/symbol breakdowns;
- top 1%, 5% and 10% contribution;
- missing-outcome rate;
- holding-horizon MFE and MAE.

The candidate rule, direction, holding horizon, entry stride and entry anchor remain frozen.

## Migration safety

The targeted `sql/migrations/2.3.0.sql` migration is additive and idempotent. It:

- adds `engine_version` to `ra_robustness_runs`;
- creates the two new robustness-v2 tables and indexes;
- adds the robustness-chunk update trigger.

It does not modify `rd_` raw tables, completed `ra_intraday_features`, frozen universes or candidate rule definitions.

The startup compatibility check now requires the full v2.3.0 robustness surface. A partially applied migration is therefore repaired rather than treated as complete.

## Retry semantics for the existing failed run

The old 239/258 progress cannot be preserved because v2.2.0 never persisted the non-base subtests. After deployment, retrying that job creates the new robustness chunks and replays the diagnostics using v2.3.0.

Once a v2.3.0 run begins, completed chunks are durable. A subsequent timeout/restart retries only incomplete/split chunks.

## Release gate executed locally

The exact working tree used for packaging passed:

- 78 automated tests;
- 3 PostgreSQL integration tests skipped locally because this assembly environment does not provide PostgreSQL/Psycopg;
- Python compilation;
- JavaScript syntax validation;
- 258 literal SQL statements inspected;
- 208 statically countable SQL bindings checked;
- 6 PostgreSQL `ON CONFLICT ... DO UPDATE` statements checked for explicit targets;
- 772 generated production-query binding checks;
- all Discovery family/direction/horizon/sampling combinations for both robustness query paths;
- raw `rd_` write-protection scan;
- credential scan.

Generated-query definition hash:

`df4c164dfe34f544`

## PostgreSQL CI gates

The repository includes GitHub Actions with PostgreSQL 16 and the real Psycopg driver. Three integration tests are intentionally gated there:

1. the complete synthetic Workbench workflow, including Robustness Lab;
2. upgrade from the shipped v2.1.1 schema through the current v2.3.0 schema;
3. upgrade from the actual shipped v2.2.0 schema to v2.3.0, verifying `ra_robustness_chunks`, `ra_robustness_samples` and the robustness engine-version column.

Deployment should proceed only from a commit whose GitHub `release-gate` is green.

## Remaining limitations

- The local assembly environment did not execute PostgreSQL integration tests; they must pass in GitHub Actions before Render deployment.
- A terminal one-date/one-symbol-bucket query can still fail if the database itself cannot execute that minimal slice within its timeout. The job will expose that exact terminal slice rather than hanging or losing other completed work.
- Historical holdout reconstruction is necessarily more expensive than development robustness because it cannot reuse the original Discovery samples, but it is now bounded and automatically splittable.
