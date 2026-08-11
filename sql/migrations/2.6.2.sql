-- Allow the worker's survivorship-safe universe-only historical backfill job.
-- This job reuses the existing historical backfill config and point-in-time
-- universe builder, but deliberately does not materialize historical features.

ALTER TABLE public.ra_jobs
    DROP CONSTRAINT IF EXISTS ra_jobs_job_type_check;

ALTER TABLE public.ra_jobs
    ADD CONSTRAINT ra_jobs_job_type_check
    CHECK (job_type = ANY (ARRAY[
        'quality_scan'::text,
        'universe_build'::text,
        'feature_build'::text,
        'discovery_scan'::text,
        'robustness_analysis'::text,
        'sealed_evaluation'::text,
        'historical_feature_backfill'::text,
        'point_in_time_universe_backfill'::text,
        'market_state_build'::text,
        'candidate_wave_build'::text
    ]));
