SELECT pg_advisory_xact_lock(hashtext('alpaca_pattern_discovery_workbench_schema_v1'));
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
    feature_version text NOT NULL DEFAULT '1.1.0',
    status text NOT NULL DEFAULT 'building' CHECK (status IN ('building','completed','failed','cancelled')),
    symbol_count integer NOT NULL DEFAULT 0,
    row_count bigint NOT NULL DEFAULT 0,
    min_trade_date date,
    max_trade_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

ALTER TABLE ra_feature_sets ALTER COLUMN feature_version SET DEFAULT '1.1.0';

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

CREATE TABLE IF NOT EXISTS ra_feature_batches (
    id bigserial PRIMARY KEY,
    feature_chunk_id bigint NOT NULL REFERENCES ra_feature_chunks(id) ON DELETE CASCADE,
    batch_number integer NOT NULL,
    symbols text[] NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    rows_written bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(feature_chunk_id, batch_number)
);
CREATE INDEX IF NOT EXISTS ra_feature_batches_status_idx
    ON ra_feature_batches(feature_chunk_id, status, batch_number);

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

-- Audited methodology fields freeze the exact entry-sampling and bucket
-- definition used by discovery, validation and sealed evaluation. Existing
-- candidates are explicitly marked legacy and cannot be promoted to a new
-- sealed test without rerunning discovery.
ALTER TABLE ra_candidate_rules
    ADD COLUMN IF NOT EXISTS entry_sampling_mode text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules
    ADD COLUMN IF NOT EXISTS entry_stride_minutes integer NOT NULL DEFAULT 1;
ALTER TABLE ra_candidate_rules
    ADD COLUMN IF NOT EXISTS entry_anchor_minute integer NOT NULL DEFAULT 570;
ALTER TABLE ra_candidate_rules
    ADD COLUMN IF NOT EXISTS rule_definition_version text NOT NULL DEFAULT 'legacy';

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

DROP TRIGGER IF EXISTS ra_feature_batches_updated_at ON ra_feature_batches;
CREATE TRIGGER ra_feature_batches_updated_at BEFORE UPDATE ON ra_feature_batches
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

DROP TRIGGER IF EXISTS ra_discovery_tasks_updated_at ON ra_discovery_tasks;
CREATE TRIGGER ra_discovery_tasks_updated_at BEFORE UPDATE ON ra_discovery_tasks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

-- ============================================================
-- Discovery engine v2: bounded samples, resumable partial scans
-- ============================================================

ALTER TABLE ra_discovery_tasks
    ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_discovery_tasks
    ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'legacy';

ALTER TABLE ra_candidate_rules
    ADD COLUMN IF NOT EXISTS statistics_method text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules
    ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'legacy';

CREATE TABLE IF NOT EXISTS ra_discovery_sample_chunks (
    id bigserial PRIMARY KEY,
    discovery_run_id uuid NOT NULL REFERENCES ra_discovery_runs(id) ON DELETE CASCADE,
    period_label text NOT NULL CHECK (period_label IN ('discovery','validation')),
    sample_stride_minutes integer NOT NULL CHECK (sample_stride_minutes > 0),
    chunk_start date NOT NULL,
    chunk_end date NOT NULL,
    bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 1024),
    bucket_end integer NOT NULL CHECK (bucket_end > 0 AND bucket_end <= 1024 AND bucket_end > bucket_start),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','split','cancelled')),
    rows_written bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(discovery_run_id,period_label,sample_stride_minutes,chunk_start,chunk_end,bucket_start,bucket_end)
);
CREATE INDEX IF NOT EXISTS ra_discovery_sample_chunks_status_idx
    ON ra_discovery_sample_chunks(discovery_run_id,status,period_label,chunk_start,bucket_start);

CREATE TABLE IF NOT EXISTS ra_discovery_samples (
    discovery_run_id uuid NOT NULL REFERENCES ra_discovery_runs(id) ON DELETE CASCADE,
    period_label text NOT NULL CHECK (period_label IN ('discovery','validation')),
    symbol_bucket smallint NOT NULL CHECK (symbol_bucket >= 0 AND symbol_bucket < 1024),
    symbol text NOT NULL,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    minute_of_day smallint NOT NULL,
    weekday_iso smallint NOT NULL,
    liquidity_tier text NOT NULL,
    ret_5m_pct double precision,
    ret_30m_pct double precision,
    relative_volume_20bar double precision,
    distance_from_cumulative_vwap_pct double precision,
    cumulative_range_position double precision,
    gap_from_previous_regular_close_pct double precision,
    previous_day_return_pct double precision,
    fwd_return_5m_pct double precision,
    fwd_return_15m_pct double precision,
    fwd_return_30m_pct double precision,
    fwd_return_60m_pct double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(discovery_run_id,period_label,symbol,bar_ts)
);
CREATE INDEX IF NOT EXISTS ra_discovery_samples_scan_idx
    ON ra_discovery_samples(discovery_run_id,period_label,trade_date,symbol_bucket);
CREATE INDEX IF NOT EXISTS ra_discovery_samples_symbol_idx
    ON ra_discovery_samples(discovery_run_id,period_label,symbol,trade_date);

CREATE TABLE IF NOT EXISTS ra_discovery_task_chunks (
    id bigserial PRIMARY KEY,
    discovery_task_id bigint NOT NULL REFERENCES ra_discovery_tasks(id) ON DELETE CASCADE,
    period_label text NOT NULL CHECK (period_label IN ('discovery','validation')),
    chunk_start date NOT NULL,
    chunk_end date NOT NULL,
    bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 1024),
    bucket_end integer NOT NULL CHECK (bucket_end > 0 AND bucket_end <= 1024 AND bucket_end > bucket_start),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','split','cancelled')),
    groups_written bigint NOT NULL DEFAULT 0,
    observations_scanned bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(discovery_task_id,period_label,chunk_start,chunk_end,bucket_start,bucket_end)
);
CREATE INDEX IF NOT EXISTS ra_discovery_task_chunks_status_idx
    ON ra_discovery_task_chunks(discovery_task_id,status,period_label,chunk_start,bucket_start);

CREATE TABLE IF NOT EXISTS ra_discovery_partials (
    discovery_task_chunk_id bigint NOT NULL REFERENCES ra_discovery_task_chunks(id) ON DELETE CASCADE,
    group_key text NOT NULL,
    group_values jsonb NOT NULL,
    observations bigint NOT NULL,
    gross_sum double precision NOT NULL,
    net_sum double precision NOT NULL,
    net_sum_squares double precision NOT NULL,
    wins bigint NOT NULL,
    positive_sum double precision NOT NULL,
    negative_sum_abs double precision NOT NULL,
    worst_pct double precision,
    histogram jsonb NOT NULL,
    symbol_counts jsonb NOT NULL,
    date_counts jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(discovery_task_chunk_id,group_key)
);
CREATE INDEX IF NOT EXISTS ra_discovery_partials_group_idx
    ON ra_discovery_partials(group_key,discovery_task_chunk_id);

CREATE TABLE IF NOT EXISTS ra_sealed_chunks (
    id bigserial PRIMARY KEY,
    job_id uuid NOT NULL REFERENCES ra_jobs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE CASCADE,
    chunk_start date NOT NULL,
    chunk_end date NOT NULL,
    bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 1024),
    bucket_end integer NOT NULL CHECK (bucket_end > 0 AND bucket_end <= 1024 AND bucket_end > bucket_start),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','split','cancelled')),
    observations bigint NOT NULL DEFAULT 0,
    gross_sum double precision NOT NULL DEFAULT 0,
    net_sum double precision NOT NULL DEFAULT 0,
    net_sum_squares double precision NOT NULL DEFAULT 0,
    wins bigint NOT NULL DEFAULT 0,
    positive_sum double precision NOT NULL DEFAULT 0,
    negative_sum_abs double precision NOT NULL DEFAULT 0,
    worst_pct double precision,
    histogram jsonb NOT NULL DEFAULT '{}'::jsonb,
    symbol_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    date_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id,candidate_id,chunk_start,chunk_end,bucket_start,bucket_end)
);
CREATE INDEX IF NOT EXISTS ra_sealed_chunks_status_idx
    ON ra_sealed_chunks(job_id,status,chunk_start,bucket_start);

DROP TRIGGER IF EXISTS ra_discovery_sample_chunks_updated_at ON ra_discovery_sample_chunks;
CREATE TRIGGER ra_discovery_sample_chunks_updated_at BEFORE UPDATE ON ra_discovery_sample_chunks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_discovery_task_chunks_updated_at ON ra_discovery_task_chunks;
CREATE TRIGGER ra_discovery_task_chunks_updated_at BEFORE UPDATE ON ra_discovery_task_chunks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_sealed_chunks_updated_at ON ra_sealed_chunks;
CREATE TRIGGER ra_sealed_chunks_updated_at BEFORE UPDATE ON ra_sealed_chunks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

-- Research Integrity + Discovery Coverage Pack 1 (schema 2.2.0)
ALTER TABLE ra_jobs DROP CONSTRAINT IF EXISTS ra_jobs_job_type_check;
ALTER TABLE ra_jobs ADD CONSTRAINT ra_jobs_job_type_check CHECK (job_type IN (
    'quality_scan','universe_build','feature_build','discovery_scan','robustness_analysis','sealed_evaluation'
));
ALTER TABLE ra_discovery_runs ADD COLUMN IF NOT EXISTS campaign_name text;
ALTER TABLE ra_discovery_runs ADD COLUMN IF NOT EXISTS hypothesis_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE ra_discovery_runs ADD COLUMN IF NOT EXISTS variant_count bigint NOT NULL DEFAULT 0;
ALTER TABLE ra_discovery_runs ADD COLUMN IF NOT EXISTS defined_variant_count bigint NOT NULL DEFAULT 0;
ALTER TABLE ra_discovery_runs ADD COLUMN IF NOT EXISTS campaign_definition_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS hypothesis_ids jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS hypothesis_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS variants_tested_campaign bigint NOT NULL DEFAULT 0;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS variants_defined_campaign bigint NOT NULL DEFAULT 0;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS multiple_testing_method text;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS multiple_testing_adjusted_p double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS discovery_p25_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS discovery_p75_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS discovery_p95_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS discovery_best_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS validation_p25_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS validation_p75_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS validation_p95_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS validation_best_pct double precision;
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS discovery_status text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS sealed_feature_set_id uuid REFERENCES ra_feature_sets(id) ON DELETE SET NULL;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS close double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS price_group text;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS ret_1m_pct double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS ret_15m_pct double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS ret_60m_pct double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS ret_from_session_open_pct double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS relative_trade_count_20bar double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS rolling_realised_volatility_30bar double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS rolling_range_30bar_pct double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS same_minute_relative_volume double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS previous_day_range_pct double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS previous_day_realised_volatility double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS activity_adjusted_return_5m double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS prior_activity_adjusted_return_5m double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS activity_impact_change_ratio double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS prior_relative_volume_20bar double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS prior_relative_trade_count_20bar double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS relative_volume_change_ratio double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS relative_trade_count_change_ratio double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS range_vs_previous_day_ratio double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS volatility_vs_previous_day_ratio double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS opening_range_high double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS opening_range_low double precision;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS opening_range_position text;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS touched_session_high boolean;
ALTER TABLE ra_discovery_samples ADD COLUMN IF NOT EXISTS touched_session_low boolean;
CREATE TABLE IF NOT EXISTS ra_robustness_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE CASCADE,
    source_feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    target_feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    mode text NOT NULL CHECK (mode IN ('development','historical_holdout')), config jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    start_date date NOT NULL,end_date date NOT NULL,observations bigint NOT NULL DEFAULT 0,verdict text,summary jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS ra_robustness_runs_candidate_idx ON ra_robustness_runs(candidate_id,created_at DESC);
CREATE TABLE IF NOT EXISTS ra_robustness_observations (
    robustness_run_id uuid NOT NULL REFERENCES ra_robustness_runs(id) ON DELETE CASCADE, delay_minutes integer NOT NULL,
    symbol text NOT NULL,bar_ts timestamptz NOT NULL,trade_date date NOT NULL,minute_of_day smallint NOT NULL,
    liquidity_tier text,price_group text,gross_return_pct double precision NOT NULL,
    PRIMARY KEY(robustness_run_id,delay_minutes,symbol,bar_ts)
);
CREATE INDEX IF NOT EXISTS ra_robustness_observations_date_idx ON ra_robustness_observations(robustness_run_id,delay_minutes,trade_date);
CREATE INDEX IF NOT EXISTS ra_robustness_observations_symbol_idx ON ra_robustness_observations(robustness_run_id,delay_minutes,symbol);
CREATE TABLE IF NOT EXISTS ra_robustness_results (
    robustness_run_id uuid NOT NULL REFERENCES ra_robustness_runs(id) ON DELETE CASCADE,result_type text NOT NULL,
    result_key text NOT NULL,metrics jsonb NOT NULL,PRIMARY KEY(robustness_run_id,result_type,result_key)
);

ALTER TABLE ra_discovery_partials ADD COLUMN IF NOT EXISTS best_pct double precision;
ALTER TABLE ra_sealed_chunks ADD COLUMN IF NOT EXISTS best_pct double precision;
