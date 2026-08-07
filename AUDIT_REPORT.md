# Alpaca Pattern Discovery Workbench 2.2.0 — Research Integrity & Coverage Audit

## Scope

This release implements the smallest high-value expansion agreed after the pre-coding inventory against the user's **Expand Alpaca Workbench Discovery Coverage** brief (`Pasted text(9).txt`). The release does not attempt to manufacture unavailable sector, quote, halt, event, relationship or overseas-market data.

The objectives are:

1. preserve completed historical Discovery definitions;
2. strengthen candidate evidence before further expansion;
3. make compatible historical/forward holdout replay possible without changing frozen rules;
4. add versioned tests for the highest-value hypotheses that can be addressed with the feature data already present;
5. record campaign/test multiplicity explicitly; and
6. keep `rd_` loader data read-only.

## Existing Discovery definitions preserved

The six previously implemented families remain unchanged in their eligibility/filter definitions:

- `time_of_day`
- `oversold_reversal`
- `momentum_continuation`
- `vwap_reversion`
- `gap_behavior`
- `volume_shock`

Existing completed candidate rows are not rewritten. Earlier staged-v2 candidates remain readable by the audited Robustness/holdout replay path.

## Coverage Pack 1

The release adds six new, separately versioned families:

| Family | Hypothesis mapping | Coverage claim | What remains absent |
|---|---|---|---|
| `dip_repair` | H01 | PARTIAL | higher-low transition, true VWAP reclaim transition, quote-spread/depth repair, market/sector recovery |
| `compression_expansion` | H03 | PARTIAL | market/sector confirmation and quote spread percentile |
| `gap_state` | H04/H05 | PARTIAL | quote liquidity, sector-relative state, explicit earnings classification |
| `activity_absorption` | H06 | PARTIAL | market/sector direction and quote/depth confirmation |
| `price_efficiency` | H07 | PARTIAL | broad-market regime and market-cap segmentation |
| `new_high_liquidity_divergence` | H12 | PARTIAL | quote spread/depth and true order-book liquidity confirmation |

These families use only information available at or before the completed signal bar. Forward outcomes remain separate target fields.

## Hypotheses deliberately not approximated

The Workbench continues to flag rather than imitate missing data:

- H09–H11: frozen 14:00/17:00 activation/trigger history from the 13.8 Research Lab.
- H13–H14: point-in-time halt/resumption events.
- H25–H27: point-in-time sector/ETF membership, weights and market capitalisation.
- H28: customer/supplier relationships and exposure.
- H29: home-market listing plus FX data.
- H30–H34: timestamped news/earnings/attention events.
- H35: secondary-offering events/pricing/dilution.
- H36: index/rebalance events and closing-auction flow estimates.

H02/H08 market-only variants are identified as the next data-ready extension once SPY/benchmark-relative features are added.

## Research-integrity changes

### Campaign accounting

Every new Discovery campaign records:

- campaign name and definition version;
- hypothesis IDs and versions;
- `variant_count`: grouped parameter combinations that actually appeared and were statistically tested;
- `defined_variant_count`: the larger parameter grid defined before seeing results, including zero-observation combinations;
- corresponding tested/defined counts on each retained candidate.

The current Bonferroni normal-approximation adjustment uses the actual statistical test count. The larger defined grid is retained as a conservative audit of search complexity rather than being silently discarded.

### Candidate distribution output

New candidates additionally record p25, p75, p95 and best observation for Discovery and validation, alongside the existing mean, median, p05, worst observation, win rate, profit factor, t-statistic and concentration metrics.

### Robustness Lab

Existing and new candidates can be replayed without rediscovery. The lab provides:

- exact return distribution;
- ordinary and date-clustered t-statistics;
- leave-one-date-out minimum net expectancy;
- date, month and year breakdowns;
- symbol, liquidity-tier and price-group breakdowns;
- top 1%, 5% and 10% return contribution;
- 20/25/30/40 bps cost sensitivity by default;
- 0/1/2/5 minute entry-delay sensitivity by default;
- relaxed/exact/tightened threshold-neighbourhood sensitivity;
- explicit candidate-signal count, missing-outcome count and missing-data rate;
- holding-horizon MFE and MAE calculated from completed post-entry bars.

Open-ended thresholds such as `>=3%` and `<-3%` are explicitly perturbed by neighbourhood testing; they are no longer incorrectly treated as unchanged.

Robustness verdicts are research-stage only: `REJECT`, `WEAK`, `PROMISING`, `HISTORICAL_HOLDOUT`.

### Compatible feature-set replay

A holdout or sealed feature set must preserve:

- the same frozen universe ID;
- timeframe;
- feed;
- adjustment;
- session;
- liquidity-tier selection;
- predictor horizons;
- time-of-day baseline definition; and
- the candidate's required outcome horizon.

Operational chunk/batch settings may differ. Both the server and Candidate UI enforce/filter this compatibility.

### Provenance deletion safeguards

A feature-set job cannot be deleted while Discovery, Robustness or sealed-test evidence depends on it. This prevents an apparently tidy cleanup from destroying the provenance required to reproduce candidate evidence.

## Known integrity limitations still visible in the app

1. **Historical universe:** current frozen universes do not yet reconstruct point-in-time active/delisted membership for earlier dates.
2. **Corporate actions:** raw prices avoid back-adjustment leakage, but explicit point-in-time split/dividend event exclusions are not yet available.
3. **Quotes:** bid-ask spread, depth and quote size are absent.
4. **Market/sector context:** benchmark-relative and point-in-time sector states require enrichment.

A historical replay using today's frozen universe is therefore evidence about that frozen population; it should not be represented as an unbiased point-in-time estimate of the entire historical US equity universe.

## Migration hardening

The 2.2.0 migration is targeted and idempotent. Startup compatibility now verifies the full coverage-pack surface rather than a small subset of columns. A partially applied migration is therefore repaired instead of being silently accepted.

The PostgreSQL CI suite contains a dedicated upgrade test that installs the exact shipped **v2.1.1 schema fixture**, runs the v2.2.0 migration path and checks the new candidate, sample, robustness and schema-version objects.

## Release gate executed locally

Against the exact source tree before packaging:

- **73 automated tests passed**.
- **2 PostgreSQL-backed tests skipped locally** because the assembly environment does not provide a PostgreSQL server/Psycopg installation.
- Python compilation passed.
- JavaScript syntax validation passed.
- **240 literal SQL statements** passed Psycopg placeholder inspection.
- **192 statically countable SQL bindings** passed placeholder/parameter matching.
- **6 `ON CONFLICT ... DO UPDATE` statements** passed explicit-conflict-target validation.
- **389 generated production-query combinations** passed binding validation.
- Raw `rd_` write-protection scan passed.
- Browser-native Candidate export regression remains in place.
- Candidate export format is now `1.1` and includes robustness output.
- No packaged credentials were found.

Generated-query definition hash at final local audit:

`ca70df12bf30ec43...`

## Mandatory database-backed release gate

The local environment cannot execute the two PostgreSQL integration tests. The included GitHub Actions workflow provisions PostgreSQL 16 and runs them with the real Psycopg driver. They cover:

1. clean schema installation → universe → features → exhaustive query planning → all Discovery families → Robustness Lab → sealed evaluation; and
2. the exact v2.1.1 → v2.2.0 schema upgrade path.

**Do not deploy the commit unless GitHub Actions `release-gate` is green.**

## Data/rerun impact

- No `rd_` raw data rebuild is required.
- Existing frozen universes remain intact.
- Existing completed feature sets contain the underlying fields required by Coverage Pack 1; they do not need rebuilding for the new Discovery families.
- Existing candidates do not need rediscovery to use Robustness Lab.
- To test the new H01/H03/H04–H07/H12 families, create a **new Discovery campaign** from a compatible completed feature set rather than modifying a completed historical run.
- A new historical/forward holdout feature set is only compatible when it preserves the frozen feature definition described above.

## Interpretation rule

Nothing produced by Discovery or Robustness Lab is labelled “validated.” Sealed evaluation remains a separate explicit action and the candidate definition cannot be re-optimised after sealed evidence is revealed.
