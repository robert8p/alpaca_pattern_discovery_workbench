-- Whole-strategy economics research infrastructure (schema 2.7.0)
-- The governing optimisation target is complete executable strategy economics,
-- not individual-signal hit rate. Additive and idempotent.

ALTER TABLE ra_jobs DROP CONSTRAINT IF EXISTS ra_jobs_job_type_check;
ALTER TABLE ra_jobs ADD CONSTRAINT ra_jobs_job_type_check CHECK (job_type IN (
    'quality_scan','universe_build','feature_build','discovery_scan','robustness_analysis','sealed_evaluation',
    'historical_feature_backfill','point_in_time_universe_backfill','market_state_build','candidate_wave_build',
    'strategy_economics_analysis','strategy_combination_analysis'
));

CREATE TABLE IF NOT EXISTS ra_strategy_economics_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE RESTRICT,
    feature_set_id uuid NOT NULL REFERENCES ra_feature_sets(id) ON DELETE RESTRICT,
    mode text NOT NULL CHECK (mode IN ('research','sealed')),
    research_stage text NOT NULL CHECK (research_stage IN ('discovery','validation','research_confirmation','custom_presealed','sealed_holdout')),
    start_date date NOT NULL,
    end_date date NOT NULL,
    config jsonb NOT NULL,
    strategy_config_hash text NOT NULL,
    engine_version text NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    classification text NOT NULL DEFAULT 'exploratory',
    summary jsonb,
    scorecard jsonb,
    regime_coverage_pct double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (end_date >= start_date)
);
CREATE INDEX IF NOT EXISTS ra_strategy_runs_candidate_idx ON ra_strategy_economics_runs(candidate_id,created_at DESC);
CREATE INDEX IF NOT EXISTS ra_strategy_runs_hash_idx ON ra_strategy_economics_runs(candidate_id,strategy_config_hash,research_stage,status);

CREATE TABLE IF NOT EXISTS ra_strategy_trades (
    strategy_run_id uuid NOT NULL REFERENCES ra_strategy_economics_runs(id) ON DELETE CASCADE,
    capital_level numeric NOT NULL,
    signal_ts timestamptz NOT NULL,
    entry_ts timestamptz,
    exit_ts timestamptz,
    trade_date date NOT NULL,
    symbol text NOT NULL,
    direction text NOT NULL CHECK (direction IN ('long','short')),
    liquidity_tier text,
    exchange text,
    sector text,
    signal_strength double precision,
    entry_price double precision,
    exit_price double precision,
    gross_return_pct double precision,
    net_return_pct double precision,
    mae_pct double precision,
    mfe_pct double precision,
    desired_notional numeric,
    filled_notional numeric,
    fill_fraction double precision,
    capacity_notional numeric,
    entry_bar_dollar_volume double precision,
    daily_dollar_volume double precision,
    round_trip_cost_bps double precision,
    estimated_cost_value numeric,
    accepted boolean NOT NULL DEFAULT false,
    rejection_reason text,
    metadata_temporal_status text,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(strategy_run_id,capital_level,signal_ts,symbol)
);
CREATE INDEX IF NOT EXISTS ra_strategy_trades_run_idx ON ra_strategy_trades(strategy_run_id,capital_level,accepted,entry_ts);
CREATE INDEX IF NOT EXISTS ra_strategy_trades_symbol_idx ON ra_strategy_trades(strategy_run_id,symbol,trade_date);

CREATE TABLE IF NOT EXISTS ra_strategy_equity_points (
    strategy_run_id uuid NOT NULL REFERENCES ra_strategy_economics_runs(id) ON DELETE CASCADE,
    capital_level numeric NOT NULL,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    equity numeric NOT NULL,
    realised_pnl numeric NOT NULL DEFAULT 0,
    open_pnl numeric NOT NULL DEFAULT 0,
    gross_exposure numeric NOT NULL DEFAULT 0,
    net_exposure numeric NOT NULL DEFAULT 0,
    open_positions integer NOT NULL DEFAULT 0,
    drawdown_pct double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(strategy_run_id,capital_level,bar_ts)
);
CREATE INDEX IF NOT EXISTS ra_strategy_equity_date_idx ON ra_strategy_equity_points(strategy_run_id,capital_level,trade_date,bar_ts);

CREATE TABLE IF NOT EXISTS ra_strategy_daily_metrics (
    strategy_run_id uuid NOT NULL REFERENCES ra_strategy_economics_runs(id) ON DELETE CASCADE,
    capital_level numeric NOT NULL,
    trade_date date NOT NULL,
    market_day boolean NOT NULL DEFAULT true,
    active_day boolean NOT NULL DEFAULT false,
    trades integer NOT NULL DEFAULT 0,
    gross_return_pct double precision,
    net_return_pct double precision,
    end_equity numeric,
    gross_turnover numeric NOT NULL DEFAULT 0,
    round_trip_turnover numeric NOT NULL DEFAULT 0,
    estimated_costs numeric NOT NULL DEFAULT 0,
    peak_gross_exposure numeric NOT NULL DEFAULT 0,
    peak_net_exposure numeric NOT NULL DEFAULT 0,
    max_intraday_drawdown_pct double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(strategy_run_id,capital_level,trade_date)
);

CREATE TABLE IF NOT EXISTS ra_strategy_metric_sets (
    strategy_run_id uuid NOT NULL REFERENCES ra_strategy_economics_runs(id) ON DELETE CASCADE,
    capital_level numeric NOT NULL,
    metric_scope text NOT NULL DEFAULT 'base',
    metrics jsonb NOT NULL,
    scorecard jsonb NOT NULL DEFAULT '{}'::jsonb,
    classification text NOT NULL DEFAULT 'exploratory',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(strategy_run_id,capital_level,metric_scope)
);

CREATE TABLE IF NOT EXISTS ra_strategy_stress_results (
    strategy_run_id uuid NOT NULL REFERENCES ra_strategy_economics_runs(id) ON DELETE CASCADE,
    capital_level numeric NOT NULL,
    entry_delay_minutes integer NOT NULL,
    round_trip_cost_bps double precision NOT NULL,
    metrics jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(strategy_run_id,capital_level,entry_delay_minutes,round_trip_cost_bps)
);

CREATE TABLE IF NOT EXISTS ra_strategy_regime_results (
    strategy_run_id uuid NOT NULL REFERENCES ra_strategy_economics_runs(id) ON DELETE CASCADE,
    capital_level numeric NOT NULL,
    regime_type text NOT NULL,
    regime_value text NOT NULL,
    observations integer NOT NULL DEFAULT 0,
    independent_events integer NOT NULL DEFAULT 0,
    net_avg_pct double precision,
    median_pct double precision,
    profit_factor double precision,
    win_rate_pct double precision,
    total_pnl numeric,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(strategy_run_id,capital_level,regime_type,regime_value)
);

CREATE TABLE IF NOT EXISTS ra_strategy_combination_specs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name text NOT NULL,
    component_strategy_run_ids jsonb NOT NULL,
    methodology jsonb NOT NULL,
    methodology_hash text NOT NULL,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft','frozen','retired')),
    frozen_at timestamptz,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ra_strategy_combination_specs_status_idx ON ra_strategy_combination_specs(status,created_at DESC);

CREATE TABLE IF NOT EXISTS ra_strategy_combination_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    combination_spec_id uuid NOT NULL REFERENCES ra_strategy_combination_specs(id) ON DELETE RESTRICT,
    mode text NOT NULL CHECK (mode IN ('research','sealed')),
    start_date date NOT NULL,
    end_date date NOT NULL,
    status text NOT NULL DEFAULT 'running' CHECK (status IN ('running','completed','failed','cancelled')),
    summary jsonb,
    classification text NOT NULL DEFAULT 'exploratory',
    created_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CHECK (end_date >= start_date)
);

ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS strategy_economics_run_id uuid REFERENCES ra_strategy_economics_runs(id) ON DELETE SET NULL;
ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS strategy_configuration jsonb;
ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS strategy_configuration_hash text;
ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS strategy_freeze_timestamp timestamptz;
ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS strategy_economics_result jsonb;
ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS sealed_strategy_result jsonb;
ALTER TABLE ra_research_ledger ADD COLUMN IF NOT EXISTS combination_spec_id uuid REFERENCES ra_strategy_combination_specs(id) ON DELETE SET NULL;

CREATE OR REPLACE FUNCTION ra_guard_frozen_strategy_ledger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.strategy_freeze_timestamp IS NOT NULL AND (
        NEW.strategy_configuration IS DISTINCT FROM OLD.strategy_configuration OR
        NEW.strategy_configuration_hash IS DISTINCT FROM OLD.strategy_configuration_hash OR
        NEW.strategy_economics_run_id IS DISTINCT FROM OLD.strategy_economics_run_id
    ) THEN
        RAISE EXCEPTION 'Frozen executable strategy methodology is immutable';
    END IF;
    IF NEW.sealed_strategy_result IS NOT NULL AND (
        NEW.strategy_freeze_timestamp IS NULL OR NEW.strategy_configuration_hash IS NULL
    ) THEN
        RAISE EXCEPTION 'A sealed strategy result cannot be recorded before the executable strategy is frozen';
    END IF;
    IF OLD.sealed_strategy_result IS NOT NULL AND NEW.sealed_strategy_result IS DISTINCT FROM OLD.sealed_strategy_result THEN
        RAISE EXCEPTION 'A recorded sealed strategy result is immutable';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_research_ledger_strategy_guard ON ra_research_ledger;
CREATE TRIGGER ra_research_ledger_strategy_guard
BEFORE UPDATE OF strategy_configuration,strategy_configuration_hash,strategy_economics_run_id,strategy_freeze_timestamp,sealed_strategy_result
ON ra_research_ledger FOR EACH ROW EXECUTE FUNCTION ra_guard_frozen_strategy_ledger();
ALTER FUNCTION ra_guard_frozen_strategy_ledger() SET search_path = public, pg_temp;

CREATE OR REPLACE FUNCTION ra_guard_frozen_combination_spec()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.frozen_at IS NOT NULL AND (
        NEW.component_strategy_run_ids IS DISTINCT FROM OLD.component_strategy_run_ids OR
        NEW.methodology IS DISTINCT FROM OLD.methodology OR
        NEW.methodology_hash IS DISTINCT FROM OLD.methodology_hash
    ) THEN
        RAISE EXCEPTION 'Frozen combined-strategy methodology is immutable';
    END IF;
    IF NEW.status='frozen' AND (NEW.frozen_at IS NULL OR NEW.methodology_hash IS NULL) THEN
        RAISE EXCEPTION 'Combination methodology must have a hash and freeze timestamp';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_strategy_combination_specs_frozen_guard ON ra_strategy_combination_specs;
CREATE TRIGGER ra_strategy_combination_specs_frozen_guard
BEFORE UPDATE ON ra_strategy_combination_specs
FOR EACH ROW EXECUTE FUNCTION ra_guard_frozen_combination_spec();
ALTER FUNCTION ra_guard_frozen_combination_spec() SET search_path = public, pg_temp;

-- Extend the research-period job guard. A sealed strategy job must use the
-- exact frozen strategy-configuration hash. Stress variants are research-only.
CREATE OR REPLACE FUNCTION ra_guard_research_job_periods()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE ctl ra_research_controls%ROWTYPE; cfg_start date; cfg_end date; cid uuid; cfg_mode text; cfg_hash text; combo uuid;
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
            RAISE EXCEPTION 'Full historical backfill execution is locked; only explicitly enabled full-history jobs may run';
        END IF;
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        IF cfg_end IS NOT NULL AND cfg_end >= ctl.sealed_start_date THEN
            RAISE EXCEPTION 'Historical research feature backfill may not cross into the sealed holdout beginning %',ctl.sealed_start_date;
        END IF;
    ELSIF NEW.job_type='point_in_time_universe_backfill' THEN
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        IF cfg_end IS NULL OR cfg_end >= ctl.sealed_start_date THEN
            RAISE EXCEPTION 'Point-in-time universe research may not include the sealed holdout beginning %',ctl.sealed_start_date;
        END IF;
    ELSIF NEW.job_type='strategy_economics_analysis' THEN
        cfg_mode := COALESCE(NEW.config->>'mode','research');
        cfg_start := NULLIF(NEW.config->>'start_date','')::date;
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        cid := NULLIF(NEW.config->>'candidate_id','')::uuid;
        cfg_hash := NULLIF(NEW.config->>'strategy_config_hash','');
        IF cfg_mode='sealed' THEN
            IF cfg_start IS NULL OR cfg_start < ctl.sealed_start_date THEN
                RAISE EXCEPTION 'Sealed whole-strategy evaluation must begin on or after %',ctl.sealed_start_date;
            END IF;
            IF cid IS NULL OR cfg_hash IS NULL OR NOT EXISTS (
                SELECT 1 FROM ra_research_ledger l
                WHERE l.candidate_id=cid AND l.strategy_freeze_timestamp IS NOT NULL
                  AND l.strategy_configuration_hash=cfg_hash
            ) THEN
                RAISE EXCEPTION 'Sealed whole-strategy evaluation requires the exact frozen strategy methodology';
            END IF;
        ELSE
            IF cfg_end IS NULL OR cfg_end >= ctl.sealed_start_date THEN
                RAISE EXCEPTION 'Strategy research may not include the sealed holdout beginning %',ctl.sealed_start_date;
            END IF;
        END IF;
    ELSIF NEW.job_type='strategy_combination_analysis' THEN
        cfg_mode := COALESCE(NEW.config->>'mode','research');
        cfg_start := NULLIF(NEW.config->>'start_date','')::date;
        cfg_end := NULLIF(NEW.config->>'end_date','')::date;
        combo := NULLIF(NEW.config->>'combination_spec_id','')::uuid;
        IF cfg_mode='sealed' THEN
            IF cfg_start IS NULL OR cfg_start < ctl.sealed_start_date OR combo IS NULL OR NOT EXISTS (
                SELECT 1 FROM ra_strategy_combination_specs s WHERE s.id=combo AND s.status='frozen' AND s.frozen_at IS NOT NULL
            ) THEN
                RAISE EXCEPTION 'Sealed combined-strategy evaluation requires a frozen combination methodology';
            END IF;
        ELSE
            IF cfg_end IS NULL OR cfg_end >= ctl.sealed_start_date THEN
                RAISE EXCEPTION 'Combined-strategy research may not include the sealed holdout beginning %',ctl.sealed_start_date;
            END IF;
        END IF;
    ELSIF NEW.job_type='sealed_evaluation' THEN
        cfg_start := NULLIF(NEW.config->>'sealed_start','')::date;
        cid := NULLIF(NEW.config->>'candidate_id','')::uuid;
        IF cfg_start IS NULL OR cfg_start < ctl.sealed_start_date THEN
            RAISE EXCEPTION 'True sealed evaluation must begin on or after %',ctl.sealed_start_date;
        END IF;
        IF cid IS NULL OR NOT EXISTS (
            SELECT 1 FROM ra_research_ledger l
            WHERE l.candidate_id=cid AND l.candidate_freeze_timestamp IS NOT NULL
              AND l.strategy_freeze_timestamp IS NOT NULL
        ) THEN
            RAISE EXCEPTION 'Candidate and executable strategy must both be frozen before sealed evaluation';
        END IF;
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_jobs_research_period_guard ON ra_jobs;
CREATE TRIGGER ra_jobs_research_period_guard
BEFORE INSERT OR UPDATE OF job_type,config ON ra_jobs
FOR EACH ROW EXECUTE FUNCTION ra_guard_research_job_periods();
ALTER FUNCTION ra_guard_research_job_periods() SET search_path = public, pg_temp;

DROP TRIGGER IF EXISTS ra_strategy_combination_specs_updated_at ON ra_strategy_combination_specs;
CREATE TRIGGER ra_strategy_combination_specs_updated_at BEFORE UPDATE ON ra_strategy_combination_specs
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

ALTER TABLE ra_strategy_economics_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_trades ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_equity_points ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_daily_metrics ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_metric_sets ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_stress_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_regime_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_combination_specs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_strategy_combination_runs ENABLE ROW LEVEL SECURITY;

INSERT INTO ra_schema_versions(version,app_version) VALUES ('2.7.0','2.7.0')
ON CONFLICT (version) DO UPDATE SET app_version=excluded.app_version,applied_at=now();
