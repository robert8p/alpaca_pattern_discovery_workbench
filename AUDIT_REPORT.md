# Pattern Discovery Workbench 2.0.0 — audit report

## Scope

Version 2.0.0 replaces the withdrawn monolithic discovery engine. The rebuild was initiated after production-sized one-minute feature scans repeatedly exceeded PostgreSQL statement timeouts.

The loader, `rd_` market data, liquidity-ranked universes and completed `ra_intraday_features` feature sets are outside the rebuild and remain compatible.

## Architecture reviewed

The v2 discovery path is:

```text
completed feature set
→ narrow sampled discovery/validation rows
→ bounded date × deterministic symbol-bucket chunks
→ persistent mergeable partial statistics
→ Python merge and shortlist
→ separate bounded sealed evaluation
```

No family/direction/horizon scan requires one whole-period grouped query. Failed chunks can split by date and then by one of 1,024 deterministic symbol buckets.

## Correctness controls

The audit covers:

- Six rule families.
- Long and short directions.
- 5-, 15-, 30- and 60-minute outcomes.
- Non-overlapping and every-bar sampling.
- Discovery, validation and sealed-period condition replay.
- Exact mean, sum-of-squares, win counts, profit-factor components, worst return and symbol/date concentration maps.
- Documented 0.1-percentage-point histogram approximation for median and fifth percentile.
- Explicit cost application and direction handling.
- Null-safe predictor conditions.
- Immutable engine, rule-definition, sampling and statistics metadata on candidates.
- Pause, resume, cancellation, stale-worker recovery, timeout splitting and transient database retries.
- Targeted schema migration from the existing 1.x workbench.
- Application-level prohibition on writes to loader-owned `rd_` tables.

## Assembly-environment release gate

The final local gate completed with:

```text
45 tests passed
1 PostgreSQL integration test skipped
Python compilation passed
JavaScript syntax validation passed
214 literal SQL statements passed placeholder inspection
167 statically countable execute bindings matched their parameter counts
196 generated production-query combinations passed binding validation
Render Blueprint and version checks passed
Raw rd_ write-protection scan passed
Credential scan passed
```

Generated-query definition hash:

```text
68555b6233a30b2926a1c789e8afb51d674cc95252077e297245efdb987c6a4c
```

The generated-query gate covers:

- 2 sample-materialisation variants.
- 96 partial-scan variants: family × direction × horizon × sampling mode.
- 96 sealed-evaluation variants using replayable structured conditions.
- 1 feature-build query.
- 1 universe-build query.

## Real PostgreSQL gate

The assembly environment did **not** contain a PostgreSQL server or the Psycopg driver, so the database-backed test could not be executed locally. It is included in `.github/workflows/ci.yml` and must pass before deployment.

The GitHub `release-gate` workflow provisions PostgreSQL 16 and executes the complete synthetic path through real Psycopg:

1. Recreates and seeds synthetic `rd_assets` and more than 30,000 one-minute `rd_bars` rows.
2. Installs the 2.0.0 schema.
3. Builds a three-symbol liquidity-ranked universe.
4. Generates the reusable feature set.
5. Asks PostgreSQL to plan all 196 generated production queries.
6. Runs all six families, both directions and all four holding horizons.
7. Verifies candidate sampling and rule-definition metadata.
8. Runs sealed evaluation.
9. Executes the repository-wide release audit.

A commit is not release-qualified until that workflow is green.

## Production preflight

The deployed dashboard performs a second PostgreSQL planning check against the actual Supabase schema before it accepts discovery or sealed-evaluation jobs. It verifies the v2 objects and plans representative sample, partial, sealed, feature and universe queries.

This preflight tests schema/query compatibility, not production-scale runtime. Runtime safety is provided by bounded chunks, hard statement and wall-clock deadlines, persistent checkpoints and automatic splitting.

## Residual limitations

- No synthetic test can reproduce the exact performance of the user's Supabase compute tier and pooled workload.
- Histogram median and fifth-percentile estimates are approximate to 0.1 percentage point; tails are clipped to ±20% for quantiles, while the worst return remains exact.
- Event-level t-statistics do not adjust for common market-factor correlation across simultaneous symbols.
- Candidate metrics are not a portfolio-capital or order-book simulation.
- Survivorship bias, spreads, slippage, short availability and multiple testing remain research risks.
- Raw-table write protection is application-enforced unless a dedicated PostgreSQL role is created.

## Release decision

The archive is suitable to enter the mandatory PostgreSQL CI gate. Deployment should occur only from the exact commit that passes that gate. Existing feature sets do not need to be rebuilt; withdrawn 1.x discovery artefacts are reset when their jobs are retried under 2.0.0.
