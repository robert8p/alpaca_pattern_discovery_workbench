ALTER TABLE ra_discovery_tasks ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_discovery_tasks ADD COLUMN IF NOT EXISTS stage text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS statistics_method text NOT NULL DEFAULT 'legacy';
ALTER TABLE ra_candidate_rules ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'legacy';

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
    id bigserial PRIMARY KEY, discovery_task_id bigint NOT NULL REFERENCES ra_discovery_tasks(id) ON DELETE CASCADE,
    period_label text NOT NULL CHECK (period_label IN ('discovery','validation')), chunk_start date NOT NULL, chunk_end date NOT NULL,
    bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 1024),
    bucket_end integer NOT NULL CHECK (bucket_end > 0 AND bucket_end <= 1024 AND bucket_end > bucket_start),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','split','cancelled')),
    groups_written bigint NOT NULL DEFAULT 0, observations_scanned bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0, error text, started_at timestamptz, completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(discovery_task_id,period_label,chunk_start,chunk_end,bucket_start,bucket_end)
);
CREATE INDEX IF NOT EXISTS ra_discovery_task_chunks_status_idx ON ra_discovery_task_chunks(discovery_task_id,status,period_label,chunk_start,bucket_start);

CREATE TABLE IF NOT EXISTS ra_discovery_partials (
    discovery_task_chunk_id bigint NOT NULL REFERENCES ra_discovery_task_chunks(id) ON DELETE CASCADE,
    group_key text NOT NULL, group_values jsonb NOT NULL, observations bigint NOT NULL,
    gross_sum double precision NOT NULL, net_sum double precision NOT NULL, net_sum_squares double precision NOT NULL,
    wins bigint NOT NULL, positive_sum double precision NOT NULL, negative_sum_abs double precision NOT NULL,
    worst_pct double precision, histogram jsonb NOT NULL, symbol_counts jsonb NOT NULL, date_counts jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(discovery_task_chunk_id,group_key)
);
CREATE INDEX IF NOT EXISTS ra_discovery_partials_group_idx ON ra_discovery_partials(group_key,discovery_task_chunk_id);

CREATE TABLE IF NOT EXISTS ra_sealed_chunks (
    id bigserial PRIMARY KEY, job_id uuid NOT NULL REFERENCES ra_jobs(id) ON DELETE CASCADE,
    candidate_id uuid NOT NULL REFERENCES ra_candidate_rules(id) ON DELETE CASCADE,
    chunk_start date NOT NULL, chunk_end date NOT NULL,
    bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 1024),
    bucket_end integer NOT NULL CHECK (bucket_end > 0 AND bucket_end <= 1024 AND bucket_end > bucket_start),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','split','cancelled')),
    observations bigint NOT NULL DEFAULT 0, gross_sum double precision NOT NULL DEFAULT 0,
    net_sum double precision NOT NULL DEFAULT 0, net_sum_squares double precision NOT NULL DEFAULT 0,
    wins bigint NOT NULL DEFAULT 0, positive_sum double precision NOT NULL DEFAULT 0,
    negative_sum_abs double precision NOT NULL DEFAULT 0, worst_pct double precision,
    histogram jsonb NOT NULL DEFAULT '{}'::jsonb, symbol_counts jsonb NOT NULL DEFAULT '{}'::jsonb,
    date_counts jsonb NOT NULL DEFAULT '{}'::jsonb, attempts integer NOT NULL DEFAULT 0,
    error text, completed_at timestamptz, updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(job_id,candidate_id,chunk_start,chunk_end,bucket_start,bucket_end)
);
CREATE INDEX IF NOT EXISTS ra_sealed_chunks_status_idx ON ra_sealed_chunks(job_id,status,chunk_start,bucket_start);

DROP TRIGGER IF EXISTS ra_discovery_sample_chunks_updated_at ON ra_discovery_sample_chunks;
CREATE TRIGGER ra_discovery_sample_chunks_updated_at BEFORE UPDATE ON ra_discovery_sample_chunks FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_discovery_task_chunks_updated_at ON ra_discovery_task_chunks;
CREATE TRIGGER ra_discovery_task_chunks_updated_at BEFORE UPDATE ON ra_discovery_task_chunks FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
DROP TRIGGER IF EXISTS ra_sealed_chunks_updated_at ON ra_sealed_chunks;
CREATE TRIGGER ra_sealed_chunks_updated_at BEFORE UPDATE ON ra_sealed_chunks FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
