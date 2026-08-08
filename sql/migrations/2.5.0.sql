-- Phase 1: Full-History Clustered Discovery infrastructure (schema 2.5.0)
-- This migration creates infrastructure only. It does not launch feature backfill,
-- market-state generation, clustered discovery, or sealed evaluation.

ALTER TABLE ra_jobs DROP CONSTRAINT IF EXISTS ra_jobs_job_type_check;
ALTER TABLE ra_jobs ADD CONSTRAINT ra_jobs_job_type_check CHECK (job_type IN (
    'quality_scan','universe_build','feature_build','discovery_scan','robustness_analysis','sealed_evaluation',
    'historical_feature_backfill','market_state_build','candidate_wave_build'
));

CREATE TABLE IF NOT EXISTS ra_research_controls (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    sealed_guard_enabled boolean NOT NULL DEFAULT true,
    sealed_start_date date NOT NULL DEFAULT DATE '2026-08-04',
    full_history_execution_enabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ra_research_controls(singleton,sealed_guard_enabled,sealed_start_date,full_history_execution_enabled)
VALUES (true,true,DATE '2026-08-04',false)
ON CONFLICT (singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS ra_research_periods (
    stage text PRIMARY KEY CHECK (stage IN ('discovery','validation','research_confirmation','sealed_holdout')),
    stage_order smallint NOT NULL UNIQUE,
    start_date date NOT NULL,
    end_date date,
    sealed boolean NOT NULL DEFAULT false,
    description text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO ra_research_periods(stage,stage_order,start_date,end_date,sealed,description) VALUES
    ('discovery',1,DATE '2025-05-04',DATE '2026-02-28',false,'Discovery: feature/candidate generation only.'),
    ('validation',2,DATE '2026-03-01',DATE '2026-05-31',false,'Validation: predeclared candidate evaluation.'),
    ('research_confirmation',3,DATE '2026-06-01',DATE '2026-08-03',false,'Research Confirmation: final pre-sealed confirmation.'),
    ('sealed_holdout',4,DATE '2026-08-04',NULL,true,'True untouched sealed holdout. Outcomes require a frozen Research Ledger candidate.')
ON CONFLICT (stage) DO UPDATE SET
    stage_order=EXCLUDED.stage_order,start_date=EXCLUDED.start_date,end_date=EXCLUDED.end_date,
    sealed=EXCLUDED.sealed,description=EXCLUDED.description;

CREATE TABLE IF NOT EXISTS ra_full_history_backfills (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid UNIQUE REFERENCES ra_jobs(id) ON DELETE SET NULL,
    name text NOT NULL,
    reference_feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    universe_run_id uuid NOT NULL REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    feature_set_id uuid REFERENCES ra_feature_sets(id) ON DELETE SET NULL,
    scope text NOT NULL CHECK (scope IN ('one_day_test','full_history')),
    source_config jsonb NOT NULL,
    feature_config jsonb NOT NULL,
    feature_definition_hash text NOT NULL,
    requested_start date NOT NULL,
    requested_end date NOT NULL,
    status text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','queued','running','paused','completed','failed','cancelled')),
    months_available integer NOT NULL DEFAULT 0,
    months_completed integer NOT NULL DEFAULT 0,
    rows_processed bigint NOT NULL DEFAULT 0,
    current_partition text,
    latest_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (requested_end >= requested_start)
);
CREATE INDEX IF NOT EXISTS ra_full_history_backfills_status_idx ON ra_full_history_backfills(status,created_at DESC);

CREATE TABLE IF NOT EXISTS ra_full_history_backfill_partitions (
    id bigserial PRIMARY KEY,
    backfill_id uuid NOT NULL REFERENCES ra_full_history_backfills(id) ON DELETE CASCADE,
    partition_start date NOT NULL,
    partition_end date NOT NULL,
    research_stage text REFERENCES ra_research_periods(stage) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    rows_processed bigint NOT NULL DEFAULT 0,
    feature_chunks_total integer NOT NULL DEFAULT 0,
    feature_chunks_completed integer NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(backfill_id,partition_start,partition_end),
    CHECK (partition_end >= partition_start)
);
CREATE INDEX IF NOT EXISTS ra_full_history_backfill_partitions_status_idx
    ON ra_full_history_backfill_partitions(backfill_id,status,partition_start);

CREATE TABLE IF NOT EXISTS ra_market_state_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    universe_run_id uuid NOT NULL REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    name text NOT NULL,
    config jsonb NOT NULL,
    market_state_version text NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    row_count bigint NOT NULL DEFAULT 0,
    min_trade_date date,
    max_trade_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ra_market_state_chunks (
    id bigserial PRIMARY KEY,
    market_state_run_id uuid NOT NULL REFERENCES ra_market_state_runs(id) ON DELETE CASCADE,
    chunk_start date NOT NULL,
    chunk_end date NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    rows_written bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(market_state_run_id,chunk_start,chunk_end)
);
CREATE INDEX IF NOT EXISTS ra_market_state_chunks_status_idx ON ra_market_state_chunks(market_state_run_id,status,chunk_start);

CREATE TABLE IF NOT EXISTS ra_market_state_features (
    market_state_run_id uuid NOT NULL REFERENCES ra_market_state_runs(id) ON DELETE CASCADE,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    minute_of_day smallint NOT NULL,
    sample_stride_minutes smallint NOT NULL DEFAULT 1,
    eligible_universe_count integer NOT NULL,

    pct_positive_1m double precision, pct_positive_5m double precision, pct_positive_15m double precision,
    pct_positive_30m double precision, pct_positive_60m double precision,
    mean_return_1m_pct double precision, mean_return_5m_pct double precision, mean_return_15m_pct double precision,
    mean_return_30m_pct double precision, mean_return_60m_pct double precision,
    median_return_1m_pct double precision, median_return_5m_pct double precision, median_return_15m_pct double precision,
    median_return_30m_pct double precision, median_return_60m_pct double precision,
    dispersion_1m_pct double precision, dispersion_5m_pct double precision, dispersion_15m_pct double precision,
    dispersion_30m_pct double precision, dispersion_60m_pct double precision,
    stddev_return_1m_pct double precision, stddev_return_5m_pct double precision, stddev_return_15m_pct double precision,
    stddev_return_30m_pct double precision, stddev_return_60m_pct double precision,
    p10_return_1m_pct double precision, p10_return_5m_pct double precision, p10_return_15m_pct double precision,
    p10_return_30m_pct double precision, p10_return_60m_pct double precision,
    p25_return_1m_pct double precision, p25_return_5m_pct double precision, p25_return_15m_pct double precision,
    p25_return_30m_pct double precision, p25_return_60m_pct double precision,
    p75_return_1m_pct double precision, p75_return_5m_pct double precision, p75_return_15m_pct double precision,
    p75_return_30m_pct double precision, p75_return_60m_pct double precision,
    p90_return_1m_pct double precision, p90_return_5m_pct double precision, p90_return_15m_pct double precision,
    p90_return_30m_pct double precision, p90_return_60m_pct double precision,

    new_session_high_count integer, new_session_high_pct double precision,
    new_session_low_count integer, new_session_low_pct double precision,
    top_20pct_session_range_count integer, top_20pct_session_range_pct double precision,
    bottom_20pct_session_range_count integer, bottom_20pct_session_range_pct double precision,

    median_relative_volume double precision,
    pct_relative_volume_gt_1 double precision,
    pct_relative_volume_gt_1_5 double precision,
    median_relative_trade_count double precision,
    pct_abnormal_volatility double precision,
    pct_abnormal_activity_adjusted_price_impact double precision,

    spy_return_1m_pct double precision, spy_return_5m_pct double precision, spy_return_15m_pct double precision,
    spy_return_30m_pct double precision, spy_return_60m_pct double precision,
    spy_distance_from_vwap_pct double precision, spy_session_range_position double precision,
    spy_relative_volume double precision, spy_realised_volatility double precision,
    qqq_return_1m_pct double precision, qqq_return_5m_pct double precision, qqq_return_15m_pct double precision,
    qqq_return_30m_pct double precision, qqq_return_60m_pct double precision,
    qqq_distance_from_vwap_pct double precision, qqq_session_range_position double precision,
    qqq_relative_volume double precision, qqq_realised_volatility double precision,

    built_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(market_state_run_id,bar_ts)
) PARTITION BY RANGE (bar_ts);
CREATE INDEX IF NOT EXISTS ra_market_state_features_lookup_idx
    ON ra_market_state_features(feature_set_id,trade_date,bar_ts);

CREATE OR REPLACE FUNCTION ra_ensure_market_state_partitions(p_start date,p_end date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE m date; next_m date; part_name text;
BEGIN
    m := date_trunc('month',p_start)::date;
    WHILE m <= p_end LOOP
        next_m := (m + interval '1 month')::date;
        part_name := format('ra_market_state_features_%s',to_char(m,'YYYYMM'));
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF ra_market_state_features FOR VALUES FROM (%L) TO (%L)',part_name,m::timestamptz,next_m::timestamptz);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',part_name);
        m := next_m;
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS ra_candidate_wave_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE RESTRICT,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    name text NOT NULL,
    config jsonb NOT NULL,
    wave_version text NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    row_count bigint NOT NULL DEFAULT 0,
    min_trade_date date,
    max_trade_date date,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz
);

CREATE TABLE IF NOT EXISTS ra_candidate_wave_chunks (
    id bigserial PRIMARY KEY,
    candidate_wave_run_id uuid NOT NULL REFERENCES ra_candidate_wave_runs(id) ON DELETE CASCADE,
    chunk_start date NOT NULL,
    chunk_end date NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    rows_written bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(candidate_wave_run_id,chunk_start,chunk_end)
);
CREATE INDEX IF NOT EXISTS ra_candidate_wave_chunks_status_idx ON ra_candidate_wave_chunks(candidate_wave_run_id,status,chunk_start);

CREATE TABLE IF NOT EXISTS ra_candidate_wave_stats (
    candidate_wave_run_id uuid NOT NULL REFERENCES ra_candidate_wave_runs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE RESTRICT,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    eligible_universe_count integer NOT NULL,
    qualifying_stock_count integer NOT NULL,
    qualifying_stock_pct double precision NOT NULL,
    tier_a_count integer NOT NULL DEFAULT 0,
    tier_b_count integer NOT NULL DEFAULT 0,
    tier_c_count integer NOT NULL DEFAULT 0,
    average_signal_strength double precision,
    median_signal_strength double precision,
    maximum_signal_strength double precision,
    signal_strength_dispersion double precision,
    signal_strength_method text,
    exchange_concentration jsonb,
    largest_exchange_share_pct double precision,
    sector_concentration jsonb,
    largest_sector_share_pct double precision,
    previous_wave_qualifying_count integer,
    change_in_qualifying_count integer,
    pct_change_in_qualifying_count double precision,
    consecutive_elevated_wave_count integer NOT NULL DEFAULT 0,
    elevated_wave_threshold_pct double precision,
    built_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(candidate_wave_run_id,bar_ts)
) PARTITION BY RANGE (bar_ts);
CREATE INDEX IF NOT EXISTS ra_candidate_wave_stats_candidate_idx
    ON ra_candidate_wave_stats(candidate_id,trade_date,bar_ts);

CREATE OR REPLACE FUNCTION ra_ensure_candidate_wave_partitions(p_start date,p_end date)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE m date; next_m date; part_name text;
BEGIN
    m := date_trunc('month',p_start)::date;
    WHILE m <= p_end LOOP
        next_m := (m + interval '1 month')::date;
        part_name := format('ra_candidate_wave_stats_%s',to_char(m,'YYYYMM'));
        EXECUTE format('CREATE TABLE IF NOT EXISTS %I PARTITION OF ra_candidate_wave_stats FOR VALUES FROM (%L) TO (%L)',part_name,m::timestamptz,next_m::timestamptz);
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',part_name);
        m := next_m;
    END LOOP;
END $$;

CREATE TABLE IF NOT EXISTS ra_research_campaigns (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_name text NOT NULL,
    discovery_run_id uuid UNIQUE REFERENCES ra_discovery_runs(id) ON DELETE SET NULL,
    feature_set_id uuid REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    universe_id uuid REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    engine_version text,
    code_rule_version text,
    parameters_searched jsonb NOT NULL DEFAULT '{}'::jsonb,
    discovery_start date, discovery_end date,
    validation_start date, validation_end date,
    research_confirmation_start date, research_confirmation_end date,
    sealed_test_start date, sealed_test_end date,
    number_candidates_tested bigint NOT NULL DEFAULT 0,
    classification text NOT NULL DEFAULT 'research',
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ra_research_campaigns_created_idx ON ra_research_campaigns(created_at DESC);

CREATE TABLE IF NOT EXISTS ra_research_ledger (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    campaign_id uuid NOT NULL REFERENCES ra_research_campaigns(id) ON DELETE CASCADE,
    campaign_name text NOT NULL,
    discovery_run_id uuid REFERENCES ra_discovery_runs(id) ON DELETE SET NULL,
    feature_set_id uuid REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    universe_id uuid REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    engine_version text,
    code_rule_version text,
    candidate_family text,
    candidate_id uuid REFERENCES ra_candidate_rules(id) ON DELETE RESTRICT,
    complete_candidate_configuration jsonb,
    parameters_searched jsonb NOT NULL DEFAULT '{}'::jsonb,
    discovery_start date, discovery_end date,
    validation_start date, validation_end date,
    research_confirmation_start date, research_confirmation_end date,
    sealed_test_start date, sealed_test_end date,
    number_candidates_tested bigint NOT NULL DEFAULT 0,
    candidate_retention_status text,
    validation_result jsonb,
    confirmation_result jsonb,
    candidate_freeze_timestamp timestamptz,
    frozen_candidate_hash text,
    sealed_test_result jsonb,
    classification text NOT NULL DEFAULT 'research',
    notes text,
    failure_reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(campaign_id,candidate_id)
);
CREATE INDEX IF NOT EXISTS ra_research_ledger_candidate_idx ON ra_research_ledger(candidate_id,candidate_freeze_timestamp);
CREATE INDEX IF NOT EXISTS ra_research_ledger_class_idx ON ra_research_ledger(classification,created_at DESC);

-- Existing campaigns/results have already been examined. Record that explicitly so
-- they can never be mistaken for untouched evidence after Phase 1.
INSERT INTO ra_research_campaigns(
    campaign_name,discovery_run_id,feature_set_id,universe_id,engine_version,code_rule_version,
    parameters_searched,discovery_start,discovery_end,validation_start,validation_end,
    research_confirmation_start,research_confirmation_end,sealed_test_start,
    number_candidates_tested,classification,notes
)
SELECT COALESCE(r.campaign_name,r.name),r.id,r.feature_set_id,f.universe_run_id,
       COALESCE(NULLIF(r.campaign_definition_version,'legacy'),'pre_phase1'),r.campaign_definition_version,
       r.config,
       NULLIF(r.config->>'discovery_start','')::date,NULLIF(r.config->>'discovery_end','')::date,
       NULLIF(r.config->>'validation_start','')::date,NULLIF(r.config->>'validation_end','')::date,
       DATE '2026-06-01',DATE '2026-08-03',DATE '2026-08-04',
       r.candidates_tested,'historical_pre_phase1',
       'Imported during Phase 1. These periods/candidates were examined before the Research Ledger existed and are not untouched evidence.'
FROM ra_discovery_runs r
JOIN ra_feature_sets f ON f.id=r.feature_set_id
ON CONFLICT (discovery_run_id) DO NOTHING;

INSERT INTO ra_research_ledger(
    campaign_id,campaign_name,discovery_run_id,feature_set_id,universe_id,engine_version,code_rule_version,
    candidate_family,candidate_id,complete_candidate_configuration,parameters_searched,
    discovery_start,discovery_end,validation_start,validation_end,
    research_confirmation_start,research_confirmation_end,sealed_test_start,
    number_candidates_tested,candidate_retention_status,validation_result,classification,notes
)
SELECT rc.id,rc.campaign_name,c.discovery_run_id,c.feature_set_id,rc.universe_id,c.engine_version,c.rule_definition_version,
       c.family,c.id,
       jsonb_build_object(
          'family',c.family,'direction',c.direction,'holding_horizon_minutes',c.holding_horizon_minutes,
          'conditions',c.conditions,'entry_sampling_mode',c.entry_sampling_mode,'entry_stride_minutes',c.entry_stride_minutes,
          'entry_anchor_minute',c.entry_anchor_minute,'rule_definition_version',c.rule_definition_version,
          'hypothesis_ids',c.hypothesis_ids,'hypothesis_version',c.hypothesis_version
       ),
       r.config,rc.discovery_start,rc.discovery_end,rc.validation_start,rc.validation_end,
       DATE '2026-06-01',DATE '2026-08-03',DATE '2026-08-04',r.candidates_tested,c.workflow_status,
       jsonb_build_object('observations',c.validation_observations,'net_avg_pct',c.validation_net_avg_pct,
          'median_pct',c.validation_median_pct,'win_rate_pct',c.validation_win_rate_pct,
          't_stat',c.validation_t_stat,'profit_factor',c.validation_profit_factor),
       'historical_pre_phase1',
       'Imported during Phase 1. Candidate is deliberately NOT frozen; development/validation data were previously viewed.'
FROM ra_candidate_rules c
JOIN ra_discovery_runs r ON r.id=c.discovery_run_id
JOIN ra_research_campaigns rc ON rc.discovery_run_id=r.id
ON CONFLICT (campaign_id,candidate_id) DO NOTHING;

CREATE OR REPLACE FUNCTION ra_guard_research_job_periods()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ctl ra_research_controls%ROWTYPE; cfg_start date; cfg_end date; cid uuid;
BEGIN
    SELECT * INTO ctl FROM ra_research_controls WHERE singleton=true;
    IF NOT FOUND OR NOT ctl.sealed_guard_enabled THEN RETURN NEW; END IF;

    IF NEW.job_type='discovery_scan' THEN
        cfg_end := COALESCE(NULLIF(NEW.config->>'validation_end','')::date,NULLIF(NEW.config->>'discovery_end','')::date);
        IF cfg_end IS NULL OR cfg_end >= ctl.sealed_start_date THEN
            RAISE EXCEPTION 'Discovery/validation jobs may not include the sealed holdout beginning %',ctl.sealed_start_date;
        END IF;
    ELSIF NEW.job_type='robustness_analysis' THEN
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        IF cfg_end IS NOT NULL AND cfg_end >= ctl.sealed_start_date THEN
            RAISE EXCEPTION 'Robustness/research-confirmation jobs may not include the sealed holdout beginning %',ctl.sealed_start_date;
        END IF;
    ELSIF NEW.job_type IN ('market_state_build','candidate_wave_build') THEN
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        IF cfg_end IS NULL OR cfg_end >= ctl.sealed_start_date THEN
            RAISE EXCEPTION '% is restricted to pre-sealed research dates ending %',NEW.job_type,ctl.sealed_start_date-1;
        END IF;
    ELSIF NEW.job_type='historical_feature_backfill' THEN
        IF COALESCE(NEW.config->>'scope','one_day_test')='full_history' AND NOT ctl.full_history_execution_enabled THEN
            RAISE EXCEPTION 'Full historical backfill execution is locked in Phase 1; only a one-day test may be queued';
        END IF;
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        IF cfg_end IS NOT NULL AND cfg_end >= ctl.sealed_start_date THEN
            RAISE EXCEPTION 'Phase 1 historical backfill may not cross into the sealed holdout beginning %',ctl.sealed_start_date;
        END IF;
    ELSIF NEW.job_type='sealed_evaluation' THEN
        cfg_start := NULLIF(NEW.config->>'sealed_start','')::date;
        IF cfg_start IS NULL OR cfg_start < ctl.sealed_start_date THEN
            RAISE EXCEPTION 'True sealed evaluation must begin on or after %',ctl.sealed_start_date;
        END IF;
        cid := NULLIF(NEW.config->>'candidate_id','')::uuid;
        IF cid IS NULL OR NOT EXISTS (
            SELECT 1 FROM ra_research_ledger l
            WHERE l.candidate_id=cid AND l.candidate_freeze_timestamp IS NOT NULL AND l.frozen_candidate_hash IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'Candidate must be frozen in the Research Ledger before sealed evaluation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_jobs_research_period_guard ON ra_jobs;
CREATE TRIGGER ra_jobs_research_period_guard
BEFORE INSERT OR UPDATE OF job_type,config ON ra_jobs
FOR EACH ROW EXECUTE FUNCTION ra_guard_research_job_periods();

CREATE OR REPLACE FUNCTION ra_guard_research_ledger_sealed_result()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.sealed_test_result IS NOT NULL AND (NEW.candidate_freeze_timestamp IS NULL OR NEW.frozen_candidate_hash IS NULL) THEN
        RAISE EXCEPTION 'A sealed result cannot be recorded before the candidate is frozen';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_research_ledger_sealed_guard ON ra_research_ledger;
CREATE TRIGGER ra_research_ledger_sealed_guard
BEFORE INSERT OR UPDATE OF sealed_test_result,candidate_freeze_timestamp,frozen_candidate_hash ON ra_research_ledger
FOR EACH ROW EXECUTE FUNCTION ra_guard_research_ledger_sealed_result();

CREATE OR REPLACE FUNCTION ra_guard_frozen_candidate_mutation()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF EXISTS (SELECT 1 FROM ra_research_ledger WHERE candidate_id=OLD.id AND candidate_freeze_timestamp IS NOT NULL) AND (
        NEW.family IS DISTINCT FROM OLD.family OR NEW.direction IS DISTINCT FROM OLD.direction OR
        NEW.holding_horizon_minutes IS DISTINCT FROM OLD.holding_horizon_minutes OR NEW.conditions IS DISTINCT FROM OLD.conditions OR
        NEW.entry_sampling_mode IS DISTINCT FROM OLD.entry_sampling_mode OR NEW.entry_stride_minutes IS DISTINCT FROM OLD.entry_stride_minutes OR
        NEW.entry_anchor_minute IS DISTINCT FROM OLD.entry_anchor_minute OR NEW.rule_definition_version IS DISTINCT FROM OLD.rule_definition_version
    ) THEN
        RAISE EXCEPTION 'Frozen Research Ledger candidates are immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_candidate_rules_frozen_guard ON ra_candidate_rules;
CREATE TRIGGER ra_candidate_rules_frozen_guard
BEFORE UPDATE OF family,direction,holding_horizon_minutes,conditions,entry_sampling_mode,entry_stride_minutes,entry_anchor_minute,rule_definition_version
ON ra_candidate_rules FOR EACH ROW EXECUTE FUNCTION ra_guard_frozen_candidate_mutation();

DROP TRIGGER IF EXISTS ra_full_history_backfills_updated_at ON ra_full_history_backfills;
CREATE TRIGGER ra_full_history_backfills_updated_at BEFORE UPDATE ON ra_full_history_backfills FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_full_history_backfill_partitions_updated_at ON ra_full_history_backfill_partitions;
CREATE TRIGGER ra_full_history_backfill_partitions_updated_at BEFORE UPDATE ON ra_full_history_backfill_partitions FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_market_state_chunks_updated_at ON ra_market_state_chunks;
CREATE TRIGGER ra_market_state_chunks_updated_at BEFORE UPDATE ON ra_market_state_chunks FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_candidate_wave_chunks_updated_at ON ra_candidate_wave_chunks;
CREATE TRIGGER ra_candidate_wave_chunks_updated_at BEFORE UPDATE ON ra_candidate_wave_chunks FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_research_campaigns_updated_at ON ra_research_campaigns;
CREATE TRIGGER ra_research_campaigns_updated_at BEFORE UPDATE ON ra_research_campaigns FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_research_ledger_updated_at ON ra_research_ledger;
CREATE TRIGGER ra_research_ledger_updated_at BEFORE UPDATE ON ra_research_ledger FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();


-- Phase-1 security hardening: the app uses a direct authenticated PostgreSQL connection,
-- so these research tables do not need anonymous/authenticated PostgREST access.
ALTER TABLE ra_research_controls ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_research_periods ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_full_history_backfills ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_full_history_backfill_partitions ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_market_state_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_market_state_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_market_state_features ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_candidate_wave_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_candidate_wave_chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_candidate_wave_stats ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_research_campaigns ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_research_ledger ENABLE ROW LEVEL SECURITY;

ALTER FUNCTION ra_ensure_market_state_partitions(date,date) SET search_path = public, pg_temp;
ALTER FUNCTION ra_ensure_candidate_wave_partitions(date,date) SET search_path = public, pg_temp;
ALTER FUNCTION ra_guard_research_job_periods() SET search_path = public, pg_temp;
ALTER FUNCTION ra_guard_research_ledger_sealed_result() SET search_path = public, pg_temp;
ALTER FUNCTION ra_guard_frozen_candidate_mutation() SET search_path = public, pg_temp;

INSERT INTO ra_schema_versions(version,app_version) VALUES ('2.5.0','2.5.0') ON CONFLICT (version) DO UPDATE SET app_version=excluded.app_version,applied_at=now();
