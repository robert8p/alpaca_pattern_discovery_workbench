# Architecture

```text
Alpaca Rapid Discovery Loader
        │
        ▼
Supabase rd_bars / rd_assets / rd_jobs
        │  SELECT only
        ▼
Pattern Workbench web service
        │
        ├── queues ra_jobs
        ├── displays reports and candidates
        └── controls pause/resume/cancel/retry
        │
        ▼
Pattern Workbench worker
        │
        ├── quality reports
        ├── liquidity-ranked universes
        ├── date-chunked feature generation
        ├── checkpointed discovery scans
        └── explicit sealed evaluations
        │
        ▼
Supabase ra_* analysis tables
```

## Service responsibilities

### Web

- HTTP Basic authentication
- Configuration validation
- Background-job creation and control
- Progress, reports, feature sets and candidate leaderboard
- No long-running analytics inside web requests, except optional feature-row estimation

### Worker

- Claims one queued job at a time
- Executes database-intensive work
- Records heartbeat and events
- Preserves completed feature chunks and discovery tasks
- Respects pause/cancel between durable units of work

### Supabase

- Stores raw loader data and durable analysis state
- Executes window functions, grouped scans and bulk feature inserts
- Is likely to be the primary compute constraint during large feature builds

## Lineage

```text
ra_universe_runs
    └── ra_feature_sets
            └── ra_discovery_runs
                    └── ra_candidate_rules
```

Deleting an upstream analysis asset cascades through dependent `ra_` assets. It does not affect `rd_` data.

## Candidate lifecycle

```text
new → shortlisted → sealed_tested
  └────────→ rejected
```

A rejected or shortlisted candidate can be restored to `new`.

## Anti-leakage measures

- Predictor windows use historical or current-bar data only.
- Forward labels use `fwd_` prefixes.
- Same-minute baselines use preceding dates only.
- Exact elapsed-time checks prevent sparse bars from masquerading as fixed-minute returns.
- Validation begins after discovery.
- Sealed evaluation is a separate job and must begin after validation, or discovery where no validation was used.
