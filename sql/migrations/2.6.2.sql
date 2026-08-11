-- PTI as-of availability provenance. Additive/idempotent; launches no jobs.
ALTER TABLE ra_point_in_time_universe_snapshots
    ADD COLUMN IF NOT EXISTS availability_reference_date date,
    ADD COLUMN IF NOT EXISTS availability_removed_symbols integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS availability_refilled_symbols integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS availability_method_version text;
