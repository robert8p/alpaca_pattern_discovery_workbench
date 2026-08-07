# Alpaca Pattern Discovery Workbench 2.2.0

A button-driven research companion to the Alpaca Rapid Discovery Loader. It reads the existing `rd_` market-data tables and writes only to versioned `ra_` research tables in the same Supabase database.

## 2.2.0 — Research Integrity + Discovery Coverage Pack 1

This release expands the Workbench without changing completed historical experiment definitions.

### Existing families preserved

The original six families remain unchanged:

- `time_of_day`
- `oversold_reversal`
- `momentum_continuation`
- `vwap_reversion`
- `gap_behavior`
- `volume_shock`

### New versioned Discovery families

The following use market-data features that were already present in completed feature sets:

- `dip_repair` — H01 partial market-data implementation.
- `compression_expansion` — H03 partial implementation.
- `gap_state` — H04/H05 opening-gap acceptance/rejection states.
- `activity_absorption` — H06 negative-space activity/price-response test.
- `price_efficiency` — H07 activity-adjusted price-impact transition.
- `new_high_liquidity_divergence` — H12 activity-confirmation proxy at session highs.

These are explicitly labelled **partial** where quote, sector, market, news, halt or other enrichment is still absent. The application does not invent those missing data sources.

## Research-integrity additions

Every new Discovery run records:

- Campaign name and campaign-definition version.
- Hypothesis IDs and hypothesis versions.
- Exact number of observed parameter variants actually tested.
- Conservative size of the full defined parameter grid, including zero-observation combinations, stored separately.
- Bonferroni-adjusted, two-sided normal-approximation significance for retained candidates.
- Discovery status: `WEAK`, `PROMISING`, or `PROGRESS_TO_FORMAL_VALIDATION`.
- Additional distribution outputs including p25, p75, p95 and best observation.

Discovery, validation and sealed evidence remain logically separate. No sealed-period statistics are shown in Discovery.

## Robustness Lab

Every candidate—including candidates produced by the earlier staged-v2 rule definition—can be sent to **Run robustness lab**.

The lab calculates:

- Exact return distribution and win/profit-factor statistics.
- Date-clustered t-statistic.
- Leave-one-date-out minimum net expectancy.
- Per-date, per-month and per-year performance.
- Per-symbol performance and concentration.
- Liquidity-tier and price-group breakdowns.
- 20/25/30/40 bps cost sensitivity by default.
- 0/1/2/5 minute entry-delay sensitivity by default.
- Relaxed/exact/tightened threshold-neighbourhood performance.
- Contribution of the best 1%, 5% and 10% of observations.
- Explicit missing-outcome rate.
- Holding-horizon MFE and MAE from completed post-entry bars.

Verdicts are deliberately research-stage labels: `REJECT`, `WEAK`, `PROMISING`, or `HISTORICAL_HOLDOUT`.

## Compatible holdouts

Robustness and sealed evaluation can replay a frozen candidate on another completed feature set when all of these match:

- Frozen analysis universe.
- Timeframe.
- Feed.
- Adjustment.
- Session.
- Required outcome horizon.
- Liquidity-tier selection.
- Predictor horizons.
- Time-of-day baseline definition.

Historical holdout dates must not overlap the original Discovery/validation development period. Sealed evaluation must begin after the development boundary. The candidate conditions and entry-sampling method are never re-optimised during replay.

## Known integrity limitations

The Workbench explicitly reports limitations it cannot yet solve from the current market-data layer:

- Historical universe membership does not yet reconstruct point-in-time active/delisted membership.
- Raw prices avoid back-adjustment leakage, but explicit point-in-time corporate-action exclusion events are not yet available.
- Quote spread/depth is absent.
- Market/sector-relative states require benchmark and point-in-time sector enrichment.

These remain visible limitations rather than being approximated from unsuitable fields.

## Candidate export

**Candidates → Download analysis export** creates a ZIP containing the candidate leaderboard, frozen conditions, run configuration, feature/universe provenance, Robustness Lab summaries and detailed robustness breakdowns suitable for uploading back to ChatGPT.

## Architecture

```text
Rapid Discovery Loader
        │
        ▼
     rd_bars  (read only)
        │
        ├───────────────┐
        ▼               │
ra_intraday_features    │
        │               │
        ▼               │
ra_discovery_samples    │
        │               │
        ▼               │
bounded partial scans   │
        │               │
        ▼               │
ra_candidate_rules      │
        ├── Robustness Lab / historical holdout
        └── explicit sealed evaluation
```

The staged Discovery engine remains timeout-resistant: samples and family scans are chunked by date and deterministic symbol bucket, with committed partials surviving pause/retry/redeployment.

## Deployment

See `DEPLOYMENT.md`. Existing `rd_` bars, feature rows, universes and completed candidate results are preserved. The first 2.2.0 startup applies the targeted idempotent migration `sql/migrations/2.2.0.sql`.

## Release validation

Run locally:

```bash
python -m compileall -q app scripts tests
node --check app/static/app.js
pytest -q
python scripts/release_audit.py
```

The repository also includes `.github/workflows/ci.yml`, which provisions PostgreSQL 16 and runs the real Psycopg end-to-end workflow. Do not deploy a commit whose `release-gate` workflow is red.
