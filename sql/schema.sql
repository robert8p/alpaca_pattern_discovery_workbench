SELECT pg_advisory_xact_lock(hashtext('alpaca_pattern_discovery_workbench_schema_v1'));
CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ra_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type text NOT NULL CHECK (job_type IN (
        'quality_scan','universe_build','feature_build','discovery_scan','sealed_evaluation'
    )),
    name text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN (
        'queued','running','pause_requested','paused','cancel_requested','cancelled','completed','failed'
    )),
    phase text,
    config jsonb NOT NULL,
    result jsonb,
    progress_current bigint NOT NULL DEFAULT 0,
    progress_total bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    claimed_by text,
    heartbeat_at timestamptz,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ra_jobs_status_created_idx ON ra_jobs(status, created_at);
CREATE INDEX IF NOT EXISTS ra_jobs_type_created_idx ON ra_jobs(job_type, created_at DESC);

CREATE TABLE IF NOT EXISTS ra_job_events (
    id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ra_jobs(id) ON DELETE CASCADE,
    level text NOT NULL DEFAULT 'info',
    event_type text NOT NULL,
    message text NOT NULL,
    details jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ra_job_events_job_created_idx ON ra_job_events(job_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ra_workers (
    worker_id text PRIMARY KEY,
    status text NOT NULL,
    current_job_id uuid,
    version text NOT NULL,
    details jsonb,
    heartbeat_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ra_quality_reports (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    name text NOT NULL,
    source_config jsonb NOT NULL,
    summary jsonb NOT NULL,
    session_inventory jsonb NOT NULL,
    daily_coverage jsonb NOT NULL,
    completeness_bands jsonb NOT NULL,
    anomalies jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS ra_universe_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    name text NOT NULL,
    source_config jsonb NOT NULL,
    selection_config jsonb NOT NULL,
    total_symbols integer NOT NULL DEFAULT 0,
    included_symbols integer NOT NULL DEFAULT 0,
    tier_a_symbols integer NOT NULL DEFAULT 0,
    tier_b_symbols integer NOT NULL DEFAULT 0,
    tier_c_symbols integer NOT NULL DEFAULT 0,
    tier_d_symbols integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ra_analysis_universe (
    universe_run_id uuid NOT NULL REFERENCES ra_universe_runs(id) ON DELETE CASCADE,
    symbol text NOT NULL,
    exchange text,
    asset_name text,
    trading_days integer NOT NULL,
    average_bars_per_day double precision,
    median_daily_dollar_volume double precision,
    average_daily_dollar_volume double precision,
    median_close double precision,
    liquidity_tier text NOT NULL CHECK (liquidity_tier IN ('A','B','C','D')),
    included boolean NOT NULL,
    rank_by_liquidity integer,
    exclusion_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(universe_run_id, symbol)
);
CREATE INDEX IF NOT EXISTS ra_analysis_universe_included_idx
    ON ra_analysis_universe(universe_run_id, included, liquidity_tier, rank_by_liquidity);

CREATE TABLE IF NOT EXISTS ra_feature_sets (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    universe_run_id uuid NOT NULL REFERENCES ra_universe_runs(id) ON DELETE CASCADE,
    name text NOT NULL,
    config jsonb NOT NULL,
    feature_version text NOT NULL DEFAULT '1.0.0',
    status text NOT NULL DEFAULT 'building' CHECK (status IN ('building','completed','failed','cancelled')),
    symbol_count integer NOT NULL DEFAULT 0,
    row_count bigint NOT NULL DEFAULT 0,
    min_trade_date date,
    max_trade_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ra_feature_chunks (
    id bigserial PRIMARY KEY,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    chunk_start date NOT NULL,
    chunk_end date NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    rows_written bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(feature_set_id, chunk_start, chunk_end)
);
CREATE INDEX IF NOT EXISTS ra_feature_chunks_status_idx ON ra_feature_chunks(feature_set_id, status, chunk_start);

CREATE TABLE IF NOT EXISTS ra_intraday_features (
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    symbol text NOT NULL,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    minute_of_day smallint NOT NULL,
    weekday_iso smallint NOT NULL,
    session_label text NOT NULL,
    session_bar_number integer NOT NULL,
    liquidity_tier text NOT NULL,

    open double precision NOT NULL,
    high double precision NOT NULL,
    low double precision NOT NULL,
    close double precision NOT NULL,
    volume bigint NOT NULL,
    trade_count bigint,
    bar_vwap double precision,
    bar_dollar_volume double precision,
    prior_bar_gap_seconds integer,

    ret_1m_pct double precision,
    ret_5m_pct double precision,
    ret_15m_pct double precision,
    ret_30m_pct double precision,
    ret_60m_pct double precision,

    session_open double precision,
    ret_from_session_open_pct double precision,
    cumulative_high double precision,
    cumulative_low double precision,
    cumulative_vwap double precision,
    distance_from_cumulative_vwap_pct double precision,
    cumulative_range_position double precision,

    previous_20bar_avg_volume double precision,
    relative_volume_20bar double precision,
    previous_20bar_avg_trade_count double precision,
    relative_trade_count_20bar double precision,
    rolling_realised_volatility_30bar double precision,
    rolling_range_30bar_pct double precision,

    same_minute_avg_volume_prior_days double precision,
    same_minute_relative_volume double precision,
    same_minute_avg_abs_return_prior_days double precision,

    previous_regular_close double precision,
    gap_from_previous_regular_close_pct double precision,
    previous_day_return_pct double precision,
    previous_day_range_pct double precision,
    previous_day_volume bigint,
    previous_day_realised_volatility double precision,

    history_5m_complete boolean NOT NULL DEFAULT false,
    history_15m_complete boolean NOT NULL DEFAULT false,
    history_30m_complete boolean NOT NULL DEFAULT false,
    history_60m_complete boolean NOT NULL DEFAULT false,
    future_5m_complete boolean NOT NULL DEFAULT false,
    future_15m_complete boolean NOT NULL DEFAULT false,
    future_30m_complete boolean NOT NULL DEFAULT false,
    future_60m_complete boolean NOT NULL DEFAULT false,

    fwd_return_5m_pct double precision,
    fwd_return_15m_pct double precision,
    fwd_return_30m_pct double precision,
    fwd_return_60m_pct double precision,
    fwd_mfe_30m_pct double precision,
    fwd_mae_30m_pct double precision,
    built_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(feature_set_id, symbol, bar_ts)
) PARTITION BY RANGE (bar_ts);

CREATE INDEX IF NOT EXISTS ra_intraday_features_scan_idx
    ON ra_intraday_features(feature_set_id, trade_date, minute_of_day, liquidity_tier);
CREATE INDEX IF NOT EXISTS ra_intraday_features_symbol_idx
    ON ra_intraday_features(feature_set_id, symbol, trade_date, minute_of_day);

CREATE OR REPLACE FUNCTION ra_ensure_feature_partitions(p_start date, p_end date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE
    v_month date := date_trunc('month', p_start)::date;
    v_next date;
    v_name text;
BEGIN
    WHILE v_month <= p_end LOOP
        v_next := (v_month + interval '1 month')::date;
        v_name := 'ra_intraday_features_' || to_char(v_month, 'YYYYMM');
        PERFORM pg_advisory_xact_lock(hashtext(v_name));
        EXECUTE format(
            'CREATE TABLE IF NOT EXISTS %I PARTITION OF ra_intraday_features FOR VALUES FROM (%L) TO (%L)',
            v_name,
            v_month::timestamp AT TIME ZONE 'UTC',
            v_next::timestamp AT TIME ZONE 'UTC'
        );
        v_month := v_next;
    END LOOP;
END;
$$;

CREATE TABLE IF NOT EXISTS ra_discovery_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    name text NOT NULL,
    config jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    candidates_tested bigint NOT NULL DEFAULT 0,
    candidates_retained integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ra_discovery_tasks (
    id bigserial PRIMARY KEY,
    discovery_run_id uuid NOT NULL REFERENCES ra_discovery_runs(id) ON DELETE CASCADE,
    family text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('long','short')),
    holding_horizon_minutes integer NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    groups_tested bigint NOT NULL DEFAULT 0,
    candidates_retained integer NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(discovery_run_id,family,direction,holding_horizon_minutes)
);
CREATE INDEX IF NOT EXISTS ra_discovery_tasks_status_idx ON ra_discovery_tasks(discovery_run_id,status,id);

CREATE TABLE IF NOT EXISTS ra_candidate_rules (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    discovery_run_id uuid NOT NULL REFERENCES ra_discovery_runs(id) ON DELETE CASCADE,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    family text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('long','short')),
    holding_horizon_minutes integer NOT NULL,
    conditions jsonb NOT NULL,
    plain_english_rule text NOT NULL,
    rank_score double precision,
    workflow_status text NOT NULL DEFAULT 'new' CHECK (workflow_status IN ('new','shortlisted','rejected','sealed_tested')),

    discovery_observations bigint NOT NULL,
    discovery_symbols integer NOT NULL,
    discovery_dates integer NOT NULL,
    discovery_gross_avg_pct double precision,
    discovery_net_avg_pct double precision,
    discovery_median_pct double precision,
    discovery_win_rate_pct double precision,
    discovery_t_stat double precision,
    discovery_profit_factor double precision,
    discovery_p05_pct double precision,
    discovery_worst_pct double precision,
    discovery_max_symbol_share_pct double precision,
    discovery_max_date_share_pct double precision,

    validation_observations bigint,
    validation_symbols integer,
    validation_dates integer,
    validation_gross_avg_pct double precision,
    validation_net_avg_pct double precision,
    validation_median_pct double precision,
    validation_win_rate_pct double precision,
    validation_t_stat double precision,
    validation_profit_factor double precision,
    validation_p05_pct double precision,
    validation_worst_pct double precision,
    validation_max_symbol_share_pct double precision,
    validation_max_date_share_pct double precision,

    sealed_start date,
    sealed_end date,
    sealed_observations bigint,
    sealed_net_avg_pct double precision,
    sealed_median_pct double precision,
    sealed_win_rate_pct double precision,
    sealed_t_stat double precision,
    sealed_profit_factor double precision,
    sealed_evaluated_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ra_candidate_rules_leaderboard_idx
    ON ra_candidate_rules(discovery_run_id, workflow_status, rank_score DESC);
CREATE INDEX IF NOT EXISTS ra_candidate_rules_feature_idx
    ON ra_candidate_rules(feature_set_id, created_at DESC);

CREATE OR REPLACE FUNCTION ra_set_updated_at()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS ra_jobs_updated_at ON ra_jobs;
CREATE TRIGGER ra_jobs_updated_at BEFORE UPDATE ON ra_jobs
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

DROP TRIGGER IF EXISTS ra_feature_chunks_updated_at ON ra_feature_chunks;
CREATE TRIGGER ra_feature_chunks_updated_at BEFORE UPDATE ON ra_feature_chunks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

DROP TRIGGER IF EXISTS ra_discovery_tasks_updated_at ON ra_discovery_tasks;
CREATE TRIGGER ra_discovery_tasks_updated_at BEFORE UPDATE ON ra_discovery_tasks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
