-- Point-in-time one-day feature-chunk provenance hotfix.
-- Full-history point-in-time backfills write ra_feature_chunk_universes explicitly.
-- One-day tests use the unchanged standard feature builder, so this trigger links
-- those chunks to the already-frozen PTI snapshot when the feature set's universe
-- is exactly that snapshot universe.

CREATE OR REPLACE FUNCTION ra_link_point_in_time_feature_chunk()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE v_job_id uuid; v_universe_run_id uuid; v_snapshot_id uuid;
BEGIN
    SELECT f.job_id,f.universe_run_id INTO v_job_id,v_universe_run_id
    FROM ra_feature_sets f WHERE f.id=NEW.feature_set_id;

    IF v_job_id IS NULL OR v_universe_run_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT s.id INTO v_snapshot_id
    FROM ra_point_in_time_universe_runs r
    JOIN ra_point_in_time_universe_snapshots s
      ON s.point_in_time_universe_run_id=r.id
    WHERE r.parent_job_id=v_job_id
      AND s.status='completed'
      AND s.snapshot_universe_run_id=v_universe_run_id
      AND NEW.chunk_start >= s.effective_start
      AND NEW.chunk_end <= s.effective_end
    ORDER BY s.snapshot_date DESC
    LIMIT 1;

    IF v_snapshot_id IS NOT NULL THEN
        INSERT INTO ra_feature_chunk_universes(feature_chunk_id,point_in_time_snapshot_id,universe_run_id)
        VALUES (NEW.id,v_snapshot_id,v_universe_run_id)
        ON CONFLICT (feature_chunk_id) DO UPDATE SET
            point_in_time_snapshot_id=excluded.point_in_time_snapshot_id,
            universe_run_id=excluded.universe_run_id;
    END IF;
    RETURN NEW;
END $$;

DROP TRIGGER IF EXISTS ra_feature_chunks_pti_provenance ON ra_feature_chunks;
CREATE TRIGGER ra_feature_chunks_pti_provenance
AFTER INSERT OR UPDATE OF chunk_start,chunk_end,feature_set_id ON ra_feature_chunks
FOR EACH ROW EXECUTE FUNCTION ra_link_point_in_time_feature_chunk();
ALTER FUNCTION ra_link_point_in_time_feature_chunk() SET search_path = public, pg_temp;

-- Backfill any already-created one-day PTI chunks.
INSERT INTO ra_feature_chunk_universes(feature_chunk_id,point_in_time_snapshot_id,universe_run_id)
SELECT c.id,s.id,f.universe_run_id
FROM ra_feature_chunks c
JOIN ra_feature_sets f ON f.id=c.feature_set_id
JOIN ra_point_in_time_universe_runs r ON r.parent_job_id=f.job_id
JOIN ra_point_in_time_universe_snapshots s
  ON s.point_in_time_universe_run_id=r.id
 AND s.status='completed'
 AND s.snapshot_universe_run_id=f.universe_run_id
 AND c.chunk_start >= s.effective_start
 AND c.chunk_end <= s.effective_end
ON CONFLICT (feature_chunk_id) DO UPDATE SET
    point_in_time_snapshot_id=excluded.point_in_time_snapshot_id,
    universe_run_id=excluded.universe_run_id;
