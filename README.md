# Alpaca Pattern Discovery Workbench 2.0.1

A button-driven companion to the **Alpaca Rapid Discovery Loader**. It connects to the same Supabase PostgreSQL database, reads the loader's `rd_` market-data tables, and writes only to its own `ra_` analysis tables.

Version 2.0.1 is the startup-corrected release of the staged v2 discovery engine for production-sized one-minute datasets. The loader, universes, completed feature sets and `rd_` bars do not need to be rebuilt.

## Why the discovery engine was rebuilt

The withdrawn 1.x engine ran one large grouped PostgreSQL query for each rule family, direction and holding period. Exact percentiles, distinct counts, concentration calculations and multiple scans of the same feature rows could exceed the database statement timeout.

The staged v2 engine removes that single-query failure point:

```text
Completed feature set
        │
        ▼
Narrow sampled rows, built once per period at the minimum required cadence
        │
        ▼
Bounded date × symbol-bucket scan chunks
        │
        ▼
Persistent mergeable partial statistics
        │
        ▼
Python merge and shortlist
        │
        ▼
Separate staged sealed evaluation
```

A completed chunk is never recomputed unless its exact slice is deliberately reset. Timeouts split the failing chunk into smaller date or deterministic symbol-bucket ranges, down to one date and one of 1,024 virtual buckets.

## Main workflows

1. **Data quality** — verify coverage, completeness and OHLC integrity.
2. **Research universe** — freeze a liquidity-ranked symbol set.
3. **Feature engineering** — build reusable leakage-aware intraday predictors and future outcome labels.
4. **Staged discovery** — materialise narrow samples, scan interpretable rule families in bounded chunks and merge sufficient statistics.
5. **Candidate workflow** — inspect, shortlist, reject or restore rules.
6. **Sealed evaluation** — calculate an untouched result only after explicit promotion.

## Discovery v2 details

### Sample materialisation

For discovery and validation separately, the engine copies only the columns required by the rule families into `ra_discovery_samples`. It stores all requested forward-return columns on the same row and samples once at the smallest required cadence. Each holding-period task then applies its own cadence. This avoids duplicating the predictors four times when 5-, 15-, 30- and 60-minute outcomes are selected.

Non-overlapping mode evaluates entries at the holding-period cadence:

| Holding period | Entry cadence |
|---|---|
| 5 minutes | every 5 minutes |
| 15 minutes | every 15 minutes |
| 30 minutes | every 30 minutes |
| 60 minutes | every 60 minutes |

`all_bars` remains available, but it deliberately permits overlapping outcomes and is slower.

### Partial scans

Every family × direction × horizon × period is split by:

- Configurable calendar-day chunks, default 3 days.
- Configurable initial symbol shards, default 4.
- Automatic binary splitting after a timeout.

Each chunk stores mergeable statistics rather than complete trade rows:

- observation count
- gross and net sum
- net sum of squares
- wins
- positive and negative return sums
- exact worst result
- 0.1-percentage-point histogram
- exact symbol counts
- exact date counts

This supports mean, approximate median, approximate fifth percentile, t-statistic, profit factor and exact concentration metrics without a whole-period percentile sort.

### Statistical limitations

- Histogram median and fifth percentile are approximate to 0.1 percentage point; extreme tails are clipped at ±20% for quantiles. Exact worst return is stored separately.
- Event-level t-statistics do not correct for cross-symbol market correlation.
- Candidate metrics are not a portfolio-capital simulation.
- Survivorship, spread, short availability and multiple-testing risks still require deeper validation.

## Raw-data protection

Application and release-gate scans reject writes to `rd_` tables. The workbench writes to `ra_` only. Stronger database-level enforcement can be added by using a dedicated PostgreSQL role with read-only access to `rd_` and write access to `ra_`.

## Existing data compatibility

- Existing `rd_bars`: preserved.
- Existing universe runs: preserved.
- Existing completed feature sets: preserved; feature definition remains `1.1.0` because its calculation has not changed.
- Existing 1.x discovery jobs: retryable. On first staged-v2 retry, only withdrawn discovery samples/tasks/candidates are reset; the selected feature set remains intact.

## Deployment

Read [`DEPLOYMENT.md`](DEPLOYMENT.md). Do not deploy a commit until its GitHub `release-gate` Action is green.

## Local commands

```bash
python -m pip install -r requirements-dev.txt
pytest -q
python -m compileall -q app scripts tests
node --check app/static/app.js
python scripts/release_audit.py
```

A real PostgreSQL 16 integration test is included and runs in GitHub Actions. It is skipped locally unless `RUN_POSTGRES_INTEGRATION=1` and `TEST_DATABASE_URL` are supplied.
