# Alpaca Pattern Discovery Workbench 2.1.0 — Release Audit

## Scope

Version 2.1.0 adds a read-only Candidate Analysis Export to the completed staged v2 discovery engine. It does not change rule discovery, feature generation, sealed evaluation or the database schema.

## Export behaviour

The Candidate tab now provides **Download analysis export**. The endpoint respects the selected discovery-run and workflow-status filters and exports all matching candidates, including candidates beyond the dashboard's display limit.

The ZIP contains:

- `SUMMARY.md`
- `candidates.csv`
- `candidates.json`
- `discovery_runs.json`
- `discovery_tasks.csv`
- `feature_sets.json`
- `universes.json`
- `universe_symbols.csv`
- `README.txt`
- `ANALYSIS_PROMPT.txt`
- `manifest.json`

Candidate CSV rows are enriched with the frozen discovery/validation dates, round-trip cost assumption, minimum evidence thresholds and concentration limits from the discovery run.

## Data-safety review

- Export code only reads `ra_` analysis tables.
- No `rd_` write operations were introduced.
- No migration is required.
- Existing completed discovery results are immediately exportable after deployment.
- No database credentials, connection strings or service secrets are included in the export.

## Release gate

The exact packaged tree passed locally:

- 51 automated tests passed.
- 1 real-PostgreSQL integration test remains gated to CI because this assembly environment does not provide a PostgreSQL service.
- Python compilation passed.
- JavaScript syntax validation passed.
- 220 literal SQL statements inspected.
- 173 statically countable SQL bindings checked.
- 5 `ON CONFLICT ... DO UPDATE` statements checked for explicit conflict targets.
- 196 generated discovery-query combinations validated.
- Candidate-export ZIP structure, CSV flattening, JSON condition preservation and filename behaviour tested.
- Candidate export endpoint statically verified as read-only.
- Raw `rd_` write-protection scan passed.

## Remaining deployment proof

The included GitHub Actions release gate should still be green before deployment. The candidate-export feature itself is a synchronous read-only web endpoint; it does not depend on the background worker to generate the export once candidates already exist.
