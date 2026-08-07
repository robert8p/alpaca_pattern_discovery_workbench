-- Research Integrity + Discovery Coverage Pack 1
-- Non-destructive migration: rd_ raw tables and existing feature rows are untouched.

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
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE CASCADE,
    source_feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    target_feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE CASCADE,
    mode text NOT NULL CHECK (mode IN ('development','historical_holdout')),
    config jsonb NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    start_date date NOT NULL,
    end_date date NOT NULL,
    observations bigint NOT NULL DEFAULT 0,
    verdict text,
    summary jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);
CREATE INDEX IF NOT EXISTS ra_robustness_runs_candidate_idx ON ra_robustness_runs(candidate_id,created_at DESC);

CREATE TABLE IF NOT EXISTS ra_robustness_observations (
    robustness_run_id uuid NOT NULL REFERENCES ra_robustness_runs(id) ON DELETE CASCADE,
    delay_minutes integer NOT NULL,
    symbol text NOT NULL,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    minute_of_day smallint NOT NULL,
    liquidity_tier text,
    price_group text,
    gross_return_pct double precision NOT NULL,
    PRIMARY KEY(robustness_run_id,delay_minutes,symbol,bar_ts)
);
CREATE INDEX IF NOT EXISTS ra_robustness_observations_date_idx ON ra_robustness_observations(robustness_run_id,delay_minutes,trade_date);
CREATE INDEX IF NOT EXISTS ra_robustness_observations_symbol_idx ON ra_robustness_observations(robustness_run_id,delay_minutes,symbol);

CREATE TABLE IF NOT EXISTS ra_robustness_results (
    robustness_run_id uuid NOT NULL REFERENCES ra_robustness_runs(id) ON DELETE CASCADE,
    result_type text NOT NULL,
    result_key text NOT NULL,
    metrics jsonb NOT NULL,
    PRIMARY KEY(robustness_run_id,result_type,result_key)
);

ALTER TABLE ra_discovery_partials ADD COLUMN IF NOT EXISTS best_pct double precision;
ALTER TABLE ra_sealed_chunks ADD COLUMN IF NOT EXISTS best_pct double precision;
