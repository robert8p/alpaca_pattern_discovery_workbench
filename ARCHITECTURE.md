# Architecture — Pattern Discovery Workbench 2.3.0

## Design objective

Consolidate US-equity Discovery in one Workbench while keeping raw collection, exploratory Discovery, robustness analysis and sealed evidence logically separate.

## Data ownership

### Loader-owned, read only

- `rd_bars`
- `rd_assets`
- other `rd_` loader tables

Application code must never INSERT/UPDATE/DELETE/DDL these tables.

### Workbench-owned

Core layers:

- `ra_quality_reports`
- `ra_universe_runs`
- `ra_analysis_universe`
- `ra_feature_sets`
- `ra_intraday_features`
- `ra_discovery_runs`
- `ra_discovery_samples`
- `ra_discovery_tasks` / chunks / partials
- `ra_candidate_rules`
- `ra_robustness_runs`
- `ra_robustness_observations` (legacy robustness evidence)
- `ra_robustness_chunks`
- `ra_robustness_samples`
- `ra_robustness_results`
- `ra_sealed_chunks`

## Discovery methodology

The v2 engine materialises a narrow sampled dataset once per run. Rule-family scans are bounded by date and deterministic symbol bucket. Each chunk writes mergeable partial statistics rather than calculating whole-period percentiles in one query.

2.2.0 leaves all legacy-family definitions unchanged and adds versioned market-data-only families for H01, H03, H04/H05, H06, H07 and H12.

The run stores two distinct counts:

- `variant_count` / `candidates_tested`: grouped parameter combinations that actually occurred and were statistically examined.
- `defined_variant_count`: the complete parameter grid defined before looking at results, including zero-observation combinations.

Multiple-testing adjustment uses the exact number of statistical tests actually performed; the larger defined grid is retained as a conservative search-space audit.

## Robustness Lab

Robustness is a separate background job. v2.3.0 replays frozen conditions in bounded variant × date × symbol-bucket chunks. Development mode reads the already-materialised Discovery samples; cross-feature holdouts use bucket-bounded feature queries. Timed-out buckets split automatically, while completed buckets and observations remain committed.

It supports development-period diagnostics and a non-overlapping historical holdout on another compatible feature set.

Compatibility requires the same frozen universe, timeframe, feed, adjustment, session, liquidity-tier selection, predictor horizons, time-of-day baseline definition and required outcome horizon. Operational chunk/batch sizes may differ.

Robustness additionally reports exact holding-horizon MFE/MAE, missing-outcome rate, date/month/year breakdowns, clustered evidence, leave-one-date-out stability and sensitivity tests.

Detailed outputs are stored separately from the candidate definition so Robustness Lab never mutates the candidate.

## Sealed evaluation

Sealed evaluation remains explicit. A candidate's frozen conditions, direction, horizon, sampling stride and anchor are replayed without optimisation. A newer compatible feature set may be selected, but it must use the same frozen universe and the sealed dates must begin after the candidate's development boundary.

## Versioning

- Application version: `2.3.0`
- Schema version: `2.3.0`
- Discovery engine version: `2.2.0`
- New rule-definition version: `2026-08-coverage-pack1-v1`
- Legacy staged-v2 candidates remain readable by the audited Robustness/holdout replay paths.

Migration is targeted and idempotent. A PostgreSQL advisory lock serialises startup migration between the web and worker services.
