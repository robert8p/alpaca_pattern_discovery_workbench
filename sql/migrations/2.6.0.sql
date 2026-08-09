-- Point-in-time historical universe and liquidity-tier infrastructure.
-- Additive and idempotent. This migration does not launch historical feature
-- backfill, Discovery, robustness, or sealed evaluation.

CREATE TABLE IF NOT EXISTS ra_point_in_time_universe_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_job_id uuid NOT NULL UNIQUE REFERENCES ra_jobs(id) ON DELETE CASCADE,
    reference_universe_run_id uuid NOT NULL REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    name text NOT NULL,
    requested_start date NOT NULL,
    requested_end date NOT NULL,
    cadence text NOT NULL DEFAULT 'monthly' CHECK (cadence IN ('monthly','single_date')),
    lookback_calendar_days integer NOT NULL DEFAULT 61 CHECK (lookback_calendar_days BETWEEN 15 AND 366),
    source_config jsonb NOT NULL,
    selection_config jsonb NOT NULL,
    methodology_version text NOT NULL DEFAULT '1.0.0',
    metadata_temporal_status text NOT NULL DEFAULT 'current_reference_structural_metadata',
    status text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned','running','completed','failed','cancelled')),
    snapshots_total integer NOT NULL DEFAULT 0,
    snapshots_completed integer NOT NULL DEFAULT 0,
    earliest_usable_date date,
    latest_snapshot_date date,
    latest_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CHECK (requested_end >= requested_start)
);
CREATE INDEX IF NOT EXISTS ra_pti_universe_runs_status_idx
    ON ra_point_in_time_universe_runs(status,created_at DESC);

CREATE TABLE IF NOT EXISTS ra_point_in_time_universe_snapshots (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    point_in_time_universe_run_id uuid NOT NULL REFERENCES ra_point_in_time_universe_runs(id) ON DELETE CASCADE,
    snapshot_date date NOT NULL,
    effective_start date NOT NULL,
    effective_end date NOT NULL,
    lookback_start date NOT NULL,
    lookback_end date NOT NULL,
    child_job_id uuid REFERENCES ra_jobs(id) ON DELETE SET NULL,
    snapshot_universe_run_id uuid REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','cancelled')),
    total_symbols integer NOT NULL DEFAULT 0,
    included_symbols integer NOT NULL DEFAULT 0,
    tier_a_symbols integer NOT NULL DEFAULT 0,
    tier_b_symbols integer NOT NULL DEFAULT 0,
    tier_c_symbols integer NOT NULL DEFAULT 0,
    tier_d_symbols integer NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(point_in_time_universe_run_id,snapshot_date),
    CHECK (effective_end >= effective_start),
    CHECK (lookback_end < snapshot_date),
    CHECK (lookback_end >= lookback_start)
);
CREATE INDEX IF NOT EXISTS ra_pti_universe_snapshots_status_idx
    ON ra_point_in_time_universe_snapshots(point_in_time_universe_run_id,status,snapshot_date);
CREATE INDEX IF NOT EXISTS ra_pti_universe_snapshots_effective_idx
    ON ra_point_in_time_universe_snapshots(point_in_time_universe_run_id,effective_start,effective_end);

CREATE TABLE IF NOT EXISTS ra_feature_chunk_universes (
    feature_chunk_id bigint PRIMARY KEY REFERENCES ra_feature_chunks(id) ON DELETE CASCADE,
    point_in_time_snapshot_id uuid NOT NULL REFERENCES ra_point_in_time_universe_snapshots(id) ON DELETE RESTRICT,
    universe_run_id uuid NOT NULL REFERENCES ra_universe_runs(id) ON DELETE RESTRICT,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ra_feature_chunk_universes_universe_idx
    ON ra_feature_chunk_universes(universe_run_id,feature_chunk_id);

ALTER TABLE ra_full_history_backfills
    ADD COLUMN IF NOT EXISTS point_in_time_universe_run_id uuid REFERENCES ra_point_in_time_universe_runs(id) ON DELETE RESTRICT;
ALTER TABLE ra_full_history_backfill_partitions
    ADD COLUMN IF NOT EXISTS point_in_time_snapshot_count integer NOT NULL DEFAULT 0;

DROP TRIGGER IF EXISTS ra_point_in_time_universe_runs_updated_at ON ra_point_in_time_universe_runs;
CREATE TRIGGER ra_point_in_time_universe_runs_updated_at
BEFORE UPDATE ON ra_point_in_time_universe_runs
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_point_in_time_universe_snapshots_updated_at ON ra_point_in_time_universe_snapshots;
CREATE TRIGGER ra_point_in_time_universe_snapshots_updated_at
BEFORE UPDATE ON ra_point_in_time_universe_snapshots
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();

ALTER TABLE ra_point_in_time_universe_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_point_in_time_universe_snapshots ENABLE ROW LEVEL SECURITY;
ALTER TABLE ra_feature_chunk_universes ENABLE ROW LEVEL SECURITY;

-- Integrity guard: snapshot lookback data must end before the effective date.
CREATE OR REPLACE FUNCTION ra_guard_point_in_time_universe_snapshot()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.lookback_end >= NEW.snapshot_date THEN
        RAISE EXCEPTION 'Point-in-time universe lookback must end before snapshot date';
    END IF;
    IF NEW.effective_start < NEW.snapshot_date THEN
        RAISE EXCEPTION 'Point-in-time universe cannot become effective before its snapshot date';
    END IF;
    RETURN NEW;
END $$;
DROP TRIGGER IF EXISTS ra_point_in_time_universe_snapshot_guard ON ra_point_in_time_universe_snapshots;
CREATE TRIGGER ra_point_in_time_universe_snapshot_guard
BEFORE INSERT OR UPDATE OF snapshot_date,effective_start,lookback_end
ON ra_point_in_time_universe_snapshots
FOR EACH ROW EXECUTE FUNCTION ra_guard_point_in_time_universe_snapshot();
ALTER FUNCTION ra_guard_point_in_time_universe_snapshot() SET search_path = public, pg_temp;
