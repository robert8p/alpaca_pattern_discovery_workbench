# Deployment and upgrade guide — v2.3.0

## Purpose of this upgrade

2.3.0 adds the bounded/resumable Robustness Engine v2 on top of the existing 2.2.0 Research Integrity + Coverage Pack. It preserves existing data.

2.2.0 adds the Research Integrity + Discovery Coverage Pack 1, Robustness Lab and compatible cross-feature-set holdouts. It does not require rebuilding the existing loader data, frozen universes, completed feature sets or completed historical candidates.

## 1. Replace the repository

1. Extract `alpaca_pattern_discovery_workbench_v2.3.0.zip`.
2. Replace the contents of the existing private Workbench GitHub repository.
3. Include hidden files, especially `.github/workflows/ci.yml` and `.python-version`.
4. Commit and push.

## 2. Require the GitHub release gate

Open **GitHub → Actions → release-gate** and wait for the pushed commit to complete successfully.

The CI workflow starts PostgreSQL 16 and exercises schema migration, universe creation, feature generation, generated-query planning, Discovery, Robustness Lab and sealed replay using the real Psycopg driver.

Do not deploy the commit if this workflow is red.

## 3. Render environment

Both services must continue to use the same writable Supabase Primary Session-pooler connection string on port 5432.

Required shared settings include:

```text
DATABASE_URL=<same Primary Session-pooler URL used by the Workbench>
PYTHON_VERSION=3.12.7
AUTO_MIGRATE=true
```

The web service also requires a strong `APP_PASSWORD`.

Do not use a read replica or the transaction pooler on port 6543.

## 4. Deploy both services

Deploy the exact green GitHub commit to:

- `alpaca-pattern-workbench-web`
- `alpaca-pattern-workbench-worker`

Confirm both report version `2.3.0`.

## 5. Migration behaviour

The first 2.3.0 startup obtains the existing PostgreSQL advisory migration lock and applies the targeted idempotent migration:

```text
sql/migrations/2.2.0.sql
sql/migrations/2.3.0.sql
```

It adds only `ra_` research metadata/results and additional Discovery staging columns. It does not write to or alter `rd_bars`.

Subsequent starts detect schema version 2.3.0 and skip unnecessary startup DDL.

## 6. Post-deploy preflight

Open **System → Run checks**.

Require:

```text
SQL preflight: Passed
Raw loader bars: Found
Workbench schema: Ready
Database: writable primary
```

The local preflight validates all generated Discovery/validation/sealed query bindings plus a representative Robustness Lab query. The database preflight asks PostgreSQL to plan representative old and new family queries.

## 7. Existing candidates

Existing candidates from the staged v2 definition remain visible and are not rewritten.

They can immediately use:

- **Run robustness lab**
- Candidate analysis export
- Compatible sealed replay when the target feature set and dates satisfy the safeguards

Do not rerun a completed historical Discovery job merely to obtain Robustness Lab results.

## 8. Recommended first Robustness Lab run

For each candidate you previously shortlisted:

1. Open **Candidates**.
2. Click **Run robustness lab**.
3. Use `Development-period robustness` first.
4. Keep the source feature set selected.
5. Leave dates blank to use the original Discovery + validation development period.
6. Keep costs `20,25,30,40` bps.
7. Keep delays `0,1,2,5` minutes.
8. Keep threshold neighbourhood at `10%`.
9. Queue the analysis.

After it completes, the Candidate card displays the latest verdict. The detailed output is included in the Candidate export.

## 9. Historical holdout

To test a rule on older untouched history:

1. Ensure the Loader contains the desired historical bars.
2. Build a new feature set for those dates using the **same frozen universe and feature definition** as the original candidate (same liquidity tiers, predictor horizons and time-of-day baseline).
3. Open the candidate → **Run robustness lab**.
4. Select `Untouched historical holdout`.
5. Select the new compatible feature set.
6. Enter dates that do not overlap the original Discovery/validation development period, or leave them blank if the complete target feature set is entirely non-overlapping.

The app rejects universe, feed, timeframe, adjustment, session and outcome-horizon incompatibilities.

## 10. Forward sealed data

When a newer feature set becomes available from the same frozen universe and feature definition:

1. Click **Promote to sealed test**.
2. Select that compatible feature set.
3. Select dates beginning after the original development boundary.
4. Queue once.

The frozen rule conditions, holding horizon, sampling stride and anchor are replayed unchanged.

## 11. New expanded Discovery campaign

For new Discovery work, create a **new** Discovery job rather than editing an old completed experiment.

Recommended initial settings:

```text
Campaign: Market-Edge US Equities Pack 1
Families: all original families + H01/H03/H04-H07/H12 additions
Entry sampling: Non-overlapping
Date chunk: 3 days
Symbol shards: 4
Round-trip cost: 20 bps
```

The run records the full defined variant count and hypothesis IDs. New-family results are explicitly labelled partial where missing quote/sector/news/event data prevents full hypothesis coverage.

## 12. Troubleshooting

If migration or SQL preflight fails, do not disable preflight. Capture the exact error and fix the cause before running Discovery.

If a staged Discovery chunk times out, the existing v2 engine automatically divides the affected date/symbol slice. Completed partials remain committed.

If a Robustness Lab query is interrupted, retrying is safe; it is a derived analysis and never writes to `rd_` data.

## 10. Retrying a v2.2.0 robustness timeout

The old 239/258 in-memory progress cannot be reused because v2.2.0 did not persist non-base robustness variants. After upgrading, click **Retry** on the failed robustness job. v2.3.0 creates persistent robustness chunks and reruns the robustness variants using the bounded engine. Future failures resume only incomplete chunks.
