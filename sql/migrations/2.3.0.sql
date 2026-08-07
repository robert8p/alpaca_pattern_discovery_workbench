-- Robustness Engine v2 — bounded, resumable robustness analysis.
-- Non-destructive: existing candidates, feature sets, raw rd_ data and prior
-- robustness evidence are preserved.

ALTER TABLE ra_robustness_runs ADD COLUMN IF NOT EXISTS engine_version text NOT NULL DEFAULT 'legacy';

CREATE TABLE IF NOT EXISTS ra_robustness_chunks (
    id bigserial PRIMARY KEY,
    robustness_run_id uuid NOT NULL REFERENCES ra_robustness_runs(id) ON DELETE CASCADE,
    variant_key text NOT NULL,
    trade_date date NOT NULL,
    bucket_start integer NOT NULL CHECK (bucket_start >= 0 AND bucket_start < 1024),
    bucket_end integer NOT NULL CHECK (bucket_end > 0 AND bucket_end <= 1024 AND bucket_end > bucket_start),
    status text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','running','completed','failed','split','cancelled')),
    rows_written bigint NOT NULL DEFAULT 0,
    attempts integer NOT NULL DEFAULT 0,
    error text,
    started_at timestamptz,
    completed_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE(robustness_run_id,variant_key,trade_date,bucket_start,bucket_end)
);
CREATE INDEX IF NOT EXISTS ra_robustness_chunks_status_idx
    ON ra_robustness_chunks(robustness_run_id,status,variant_key,trade_date,bucket_start);

CREATE TABLE IF NOT EXISTS ra_robustness_samples (
    robustness_run_id uuid NOT NULL REFERENCES ra_robustness_runs(id) ON DELETE CASCADE,
    variant_key text NOT NULL,
    symbol_bucket smallint NOT NULL CHECK (symbol_bucket >= 0 AND symbol_bucket < 1024),
    symbol text NOT NULL,
    bar_ts timestamptz NOT NULL,
    trade_date date NOT NULL,
    minute_of_day smallint NOT NULL,
    liquidity_tier text,
    price_group text,
    gross_return_pct double precision,
    mfe_pct double precision,
    mae_pct double precision,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(robustness_run_id,variant_key,symbol,bar_ts)
);
CREATE INDEX IF NOT EXISTS ra_robustness_samples_scan_idx
    ON ra_robustness_samples(robustness_run_id,variant_key,trade_date,symbol_bucket);
CREATE INDEX IF NOT EXISTS ra_robustness_samples_symbol_idx
    ON ra_robustness_samples(robustness_run_id,variant_key,symbol,trade_date);

DROP TRIGGER IF EXISTS ra_robustness_chunks_updated_at ON ra_robustness_chunks;
CREATE TRIGGER ra_robustness_chunks_updated_at BEFORE UPDATE ON ra_robustness_chunks
FOR EACH ROW EXECUTE FUNCTION ra_set_updated_at();
