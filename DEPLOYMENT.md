# Step-by-step deployment

This is a **new Render application and new GitHub repository**, but it deliberately uses the **same Supabase database** as the Alpaca Rapid Discovery Loader.

Do not create a second Supabase project for this workbench unless you intentionally want an isolated copy of the raw market data.

## 1. Extract the archive

Extract the project directory locally.

The extracted directory should contain:

```text
app/
sql/
scripts/
tests/
render.yaml
requirements.txt
```

## 2. Create a separate GitHub repository

1. Sign in to GitHub.
2. Create a new repository, for example:

   ```text
   alpaca-pattern-discovery-workbench
   ```

3. Keep it private.
4. Upload all extracted files, including hidden files:
   - `.python-version`
   - `.gitignore`
   - `.env.example`
5. Commit the files.

Do not upload a real `.env` file or credentials.

## 3. Obtain the existing database URL

Use the **same `DATABASE_URL`** configured on the Rapid Discovery Loader.

The URL should point to the Supabase **Session pooler** on port 5432 and resemble:

```text
postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

You can copy the value from either loader service in Render:

1. Open `alpaca-rapid-discovery-web` or its worker.
2. Open **Environment**.
3. Copy `DATABASE_URL` securely.

The web and worker services for this workbench must use the same value.

## 4. Create a Render Blueprint

1. Open Render.
2. Select **New → Blueprint**.
3. Connect the new GitHub repository.
4. Render detects `render.yaml`.
5. Confirm two services:
   - `alpaca-pattern-workbench-web`
   - `alpaca-pattern-workbench-worker`
6. Confirm both services explicitly show:

   ```text
   PYTHON_VERSION=3.12.7
   ```

The workbench worker does not need a persistent disk because all durable state is stored in Supabase.

## 5. Enter environment variables

### Web service

Set:

```text
DATABASE_URL=<same value as loader>
APP_USERNAME=admin
APP_PASSWORD=<new long unique password>
AUTO_MIGRATE=true
DATABASE_STATEMENT_TIMEOUT_SECONDS=1800
```

### Worker service

Set:

```text
DATABASE_URL=<same value as loader>
AUTO_MIGRATE=true
WORKER_POLL_SECONDS=3
WORKER_STALE_SECONDS=300
MAX_JOB_ATTEMPTS=3
DATABASE_STATEMENT_TIMEOUT_SECONDS=1800
```

No Alpaca credentials are needed. This app reads the data already loaded into Supabase.

## 6. Deploy

Deploy both services.

Successful build logs should begin with:

```text
Python 3.12.7
```

Both services run the schema migration safely under a PostgreSQL advisory lock. The migration creates only `ra_` objects.

## 7. Verify health

Open the web-service URL and sign in using `APP_USERNAME` and `APP_PASSWORD`.

Open **System** and click **Run checks**.

Confirm:

- Supabase database connected
- `rd_bars` found
- Workbench `ra_jobs` schema ready
- Worker heartbeat is recent

If `rd_bars` is missing, the workbench is pointing at the wrong Supabase database.

## 8. First acceptance test

Use a deliberately small universe before building the full feature table.

### A. Data quality

Use:

```text
Start:       2026-07-04
End:         2026-08-03
Timeframe:   1Min
Feed:        SIP
Adjustment:  Raw
Session:     Regular
Tolerance:   95%
```

Click **Run quality scan**.

Confirm:

- The job completes.
- Invalid OHLC and nonpositive-price counts are zero or understood.
- Regular-session dates and symbols are credible.

### B. Small analysis universe

Use the same source settings, but initially set:

```text
Maximum included symbols: 25
Minimum trading days:      15
Minimum bars/day:          300
Minimum dollar volume:     US$5,000,000
```

Click **Build analysis universe**.

Inspect the symbols and confirm they are sensible liquid equities.

### C. Small feature set

Use:

```text
Universe:        the 25-symbol test universe
Start:           2026-07-04
End:             2026-08-03
Timeframe:       1Min
Feed:            SIP
Adjustment:      Raw
Session:         Regular
Tiers:           A and B
Chunk size:      1 day for acceptance testing
Baseline days:   10
Existing rows:   Rebuild selected slices
```

Click **Estimate**, then **Queue feature build**.

Inspect the job and confirm every date chunk completes.

### D. Small discovery scan

Use:

```text
Discovery:   2026-07-06 to 2026-07-17
Validation:  2026-07-20 to 2026-07-24
Sealed:      leave untouched for now
Directions:  Long and short
Hold:        30 and 60 minutes
Costs:       20 bps round trip
```

For a 25-symbol acceptance test, temporarily lower evidence thresholds if needed. The purpose of this run is to verify workflow behaviour, not establish a valid trading rule.

### E. Sealed test

After shortlisting a candidate, promote it with:

```text
Sealed start: 2026-07-27
Sealed end:   2026-08-03
```

The app will reject sealed dates that overlap discovery/validation or fall outside the feature set.

## 9. Scale to the substantive first run

After the acceptance test succeeds:

1. Build a new universe with **500–1,500 symbols**, ranked by dollar volume.
2. Review the included symbols and heuristic fund exclusions.
3. Build an A+B feature set using three-day chunks.
4. Retain the discovery/validation/sealed separation above.
5. Increase cost assumptions for less liquid tiers.

Supabase compute, not the Render worker, is likely to become the main constraint because feature engineering is executed inside PostgreSQL. If queries time out, reduce the feature chunk from three days to one day before increasing database compute.

## 10. Normal operation

You should not need to open the Supabase SQL editor.

Use the dashboard to:

- Run quality checks
- Build and inspect universes
- Estimate and build feature sets
- Pause, resume, cancel or retry jobs
- Run discovery scans
- Shortlist or reject candidates
- Promote a frozen candidate to a sealed test

## 11. Troubleshooting

### Worker does not appear

Check the worker logs and confirm its `DATABASE_URL` matches the web service exactly.

### Job is queued indefinitely

The worker is absent or its heartbeat is stale. Open **System** and inspect the heartbeat.

### Feature query times out

Reduce **Date chunk size** to one day and retry the failed job. Completed chunks are preserved.

### Discovery job fails on one combination

Retry the job. Completed family/direction/horizon tasks remain completed; only failed or interrupted combinations rerun.

### Wrong database

If System reports `rd_bars` missing, replace `DATABASE_URL` with the exact value used by the loader and redeploy.

### Python or `pydantic-core` build failure

Confirm both services use:

```text
PYTHON_VERSION=3.12.7
```

Then choose **Clear build cache & deploy**.

### Universe build fails with `could not determine data type of parameter $18`

Upgrade to version 1.0.1 or later. Version 1.0.0 did not explicitly type a blank optional include-regex parameter. After redeploying, open the failed job and click **Retry**. No schema migration is needed.
