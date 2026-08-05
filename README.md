# Alpaca Pattern Discovery Workbench

A separate, button-driven companion to the **Alpaca Rapid Discovery Loader**.

The loader acquires and stores market data in `rd_` tables. This workbench reads those tables and creates independent `ra_` analysis assets for quality checks, universe selection, feature engineering, interpretable pattern discovery, validation and sealed tests.

## Large feature builds

Feature generation is resumable at two levels: date chunks and symbol batches. The default of 100 symbols per SQL batch prevents a broad one-minute universe from placing the full lookback period into one PostgreSQL statement. Existing feature jobs created before v1.0.2 receive the default automatically when retried.

## Core safety boundary

Application code only issues `SELECT` statements against `rd_` tables. It writes exclusively to `ra_` tables.

Deleting the workbench, an analysis job, a feature set or a discovery run does not delete `rd_bars` or other loader data.

## Workflow

1. **Data quality** — verify date/session coverage, completeness and OHLC integrity.
2. **Analysis universe** — rank symbols using actual bars per day, median daily dollar volume and price.
3. **Feature engineering** — generate per-bar predictors and clearly separated forward outcome labels.
4. **Pattern discovery** — scan transparent rule families using discovery data and report validation degradation.
5. **Candidate decisions** — shortlist, reject or inspect exact stored conditions.
6. **Sealed evaluation** — calculate the final untouched period only after explicit promotion.

## Dashboard areas

- Overview
- Data quality
- Universe
- Features
- Discovery
- Candidates
- Jobs
- System

## Analysis tables

| Table | Purpose |
|---|---|
| `ra_jobs` | Background job queue and status |
| `ra_job_events` | Job history and errors |
| `ra_workers` | Worker heartbeat |
| `ra_quality_reports` | Saved quality results |
| `ra_universe_runs` | Frozen universe definitions |
| `ra_analysis_universe` | Symbol-level coverage, liquidity, tier and inclusion decision |
| `ra_feature_sets` | Versioned feature-set metadata |
| `ra_feature_chunks` | Resumable date chunks |
| `ra_intraday_features` | Per-bar predictors and outcome labels |
| `ra_discovery_runs` | Discovery configuration and totals |
| `ra_discovery_tasks` | Resumable family/direction/horizon scans |
| `ra_candidate_rules` | Plain-English candidates and period-specific metrics |

## Intraday predictors

The first feature version includes:

- 1-, 5-, 15-, 30- and 60-minute historical returns when supported by the candle size
- Return from the selected session open
- Cumulative high, low and VWAP
- Distance from cumulative VWAP
- Position in the cumulative session range
- Prior-20-bar relative volume and relative trade count
- Rolling realised volatility and range
- Same-minute volume baseline using prior days only
- Previous regular close and opening gap
- Previous-day return, range, volume and volatility
- Time of day, weekday and liquidity tier

Forward-return fields are stored separately with `fwd_` prefixes and are outcome labels, not predictors.

## Discovery families

- Time of day and weekday
- Oversold / prior-decline conditions
- Prior-rise / momentum conditions
- VWAP deviation
- Opening-gap behaviour
- Relative-volume shocks

Every candidate stores the exact condition JSON and a plain-English version. The scanner applies minimum observations, symbol coverage, date coverage, concentration limits and round-trip costs.

## Important interpretation limits

- Candidate results are **event-level conditional returns**, not a portfolio simulation.
- Overlapping signals are not converted into a capital-constrained equity curve in this version.
- The discovery engine reduces curve fitting through broad buckets, concentration controls and validation, but does not make multiple-testing risk disappear.
- The asset-name “exclude likely funds” option is heuristic and may exclude some trusts or REIT-like securities. Review the frozen universe.
- Current-active asset metadata can still create survivorship bias in longer historical research.
- One month of data is suitable for pipeline validation and hypothesis generation, not final confidence.

## Runtime

- Python 3.12.7
- FastAPI web service
- Separate background worker
- PostgreSQL/Supabase
- No worker disk required

## Local commands

```bash
cp .env.example .env
python -m pip install -r requirements.txt
python scripts/init_db.py
uvicorn app.main:app --reload
```

Run the worker separately:

```bash
python -m app.worker
```

Run tests:

```bash
python -m pip install -r requirements-dev.txt
pytest -q
```

## Deployment

See [`DEPLOYMENT.md`](DEPLOYMENT.md).
