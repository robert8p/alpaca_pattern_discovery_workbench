# Architecture

```text
Alpaca Rapid Discovery Loader
        │
        ▼
Supabase rd_bars / rd_assets
        │  SELECT only
        ▼
Pattern Workbench web
        │
        ├── validates typed configurations
        ├── runs database SQL preflight
        ├── queues ra_jobs
        └── controls pause/resume/cancel/retry
        │
        ▼
Pattern Workbench worker
        │
        ├── quality scans
        ├── liquidity-ranked universes
        ├── date- and symbol-batched features
        ├── checkpointed discovery
        └── explicit sealed evaluations
        │
        ▼
Supabase ra_* analysis tables
```

## Trust boundaries

### Raw-data boundary

The workbench application issues reads only against `rd_` objects. All analysis writes use `ra_` objects. A static release check rejects `INSERT`, `UPDATE`, `DELETE`, `CREATE`, `DROP` or `ALTER` operations against `rd_` names.

### Web boundary

The web service handles authentication, typed configuration, preflight, job creation, inspection and controls. Long-running analysis never executes inside a normal web request.

### Worker boundary

The worker claims one durable job at a time. Feature work checkpoints at date-chunk and symbol-batch level. Discovery checkpoints at family, direction and holding-horizon level.

### Database boundary

Supabase stores raw bars, analysis lineage and durable recovery state. PostgreSQL performs the window functions and grouped statistics and is normally the main compute constraint.

## SQL release gate

There are three layers:

1. **Binding parser:** every parameterised SQL string is checked using Psycopg's `%s/%b/%t/%%` grammar and exact parameter count.
2. **Generated-query preflight:** all six families × two directions × four horizons × two sampling modes are generated for grouped and exact statistics. Feature and universe INSERT queries are also generated.
3. **Database preflight:** PostgreSQL is asked to `EXPLAIN` the production queries before discovery or sealed-test jobs are accepted. CI uses exhaustive mode; the dashboard uses a representative fast mode.

The query-definition hash is exposed in System checks so the deployed web and worker methodology can be identified.

## Analysis lineage

```text
ra_universe_runs
    └── ra_feature_sets
            └── ra_discovery_runs
                    └── ra_candidate_rules
```

The dashboard blocks deletion of an upstream object while dependent downstream objects exist. Direct SQL deletion remains governed by the schema's foreign keys and should not be used in normal operation.

## Anti-leakage and consistency controls

- Predictors use current or historical data only.
- Future labels use `fwd_` prefixes.
- Same-minute baselines use preceding dates only.
- Exact elapsed-time checks prevent sparse bars from masquerading as fixed-minute returns.
- Discovery, validation and sealed periods share the same condition boundaries.
- Candidate metadata freezes entry sampling mode, stride and anchor.
- Round-trip costs are applied identically before median, win rate, t-statistic, profit factor and tail calculations.
- Sealed evaluation rejects legacy candidates.
- Validation begins strictly after discovery; sealed evaluation begins after validation or discovery where no validation period exists.
- Interpretable rule families currently require regular-session feature sets.

## Job resilience

### Features

- Date chunks
- Symbol batches
- Persistent completion state
- Server-side timeout and independent wall-clock watchdog
- Pause/cancel monitoring
- Automatic batch splitting down to one symbol
- Deadlock, serialization and lock-timeout retries
- Advisory locking per feature set

### Discovery

- Family × direction × horizon tasks
- Server-side timeout and wall-clock watchdog
- Pause/cancel monitoring
- Timeout retry with jitter
- Engine-version reset so incompatible task results are never mixed

## Schema migration

Schema version 1.1.0 adds four candidate-methodology fields. Existing compatible databases receive only this minimal migration; the full table/index DDL is not replayed during active feature work.
