from __future__ import annotations

from typing import Any

from app.db import connection
from app.features import FEATURE_VERSION
from app.history_backfill import (
    _month_partitions, backfill_feature_config, feature_definition_hash, run_historical_feature_backfill,
)
from app.market_state import _market_state_insert_sql, run_market_state_build
from app.candidate_waves import _candidate_wave_query, run_candidate_wave_build
from app.point_in_time_universe import point_in_time_source_readiness
from app.research_ledger import (
    assert_candidate_frozen, freeze_candidate, record_sealed_result, register_research_campaign, sync_candidate_ledger,
)
from app.research_policy import HISTORY_START_DATE, PRESEALED_END_DATE, SEALED_START_DATE
from app.utils import json_safe


def full_history_status() -> dict[str, Any]:
    reference_universe_id = None
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_research_controls WHERE singleton=true")
            controls = cur.fetchone()
            cur.execute("SELECT * FROM ra_research_periods ORDER BY stage_order")
            periods = cur.fetchall()
            cur.execute(
                """
                SELECT timeframe,feed,adjustment,min_bar_ts,max_bar_ts,rows_loaded,loads_completed,last_loaded_at
                FROM rd_inventory WHERE timeframe='1Min' AND feed='sip' AND adjustment='raw'
                """
            )
            inventory = cur.fetchone()
            cur.execute("SELECT * FROM ra_full_history_backfills ORDER BY created_at DESC LIMIT 1")
            backfill = cur.fetchone()
            if backfill:
                reference_universe_id = backfill.get("universe_run_id")
            if not reference_universe_id:
                cur.execute(
                    """
                    SELECT universe_run_id FROM ra_feature_sets
                    WHERE status='completed' AND config->>'timeframe'='1Min' AND config->>'feed'='sip'
                      AND config->>'adjustment'='raw' AND config->>'session'='regular'
                    ORDER BY created_at DESC LIMIT 1
                    """
                )
                reference = cur.fetchone()
                reference_universe_id = reference["universe_run_id"] if reference else None
            job = None
            current_chunk = None
            if backfill and backfill.get("job_id"):
                cur.execute("SELECT id,status,phase,progress_current,progress_total,error,attempts FROM ra_jobs WHERE id=%s", (backfill["job_id"],))
                job = cur.fetchone()
                cur.execute(
                    """
                    SELECT c.chunk_start,c.chunk_end,c.status,c.rows_written,c.attempts,c.error
                    FROM ra_feature_chunks c JOIN ra_feature_sets f ON f.id=c.feature_set_id
                    WHERE f.job_id=%s AND c.status IN ('running','failed','pending')
                    ORDER BY CASE c.status WHEN 'running' THEN 0 WHEN 'failed' THEN 1 ELSE 2 END,c.chunk_start LIMIT 1
                    """,
                    (backfill["job_id"],),
                )
                current_chunk = cur.fetchone()
            cur.execute("SELECT count(*) runs,COALESCE(sum(row_count),0) rows,max(completed_at) last_completed FROM ra_market_state_runs")
            market_state = cur.fetchone()
            cur.execute("SELECT count(*) runs,COALESCE(sum(row_count),0) rows,max(completed_at) last_completed FROM ra_candidate_wave_runs")
            waves = cur.fetchone()
            cur.execute("SELECT count(*) entries,count(*) FILTER(WHERE candidate_freeze_timestamp IS NOT NULL) frozen FROM ra_research_ledger")
            ledger = cur.fetchone()
            cur.execute("SELECT to_regclass('public.ra_market_state_features') market_state_table,to_regclass('public.ra_candidate_wave_stats') wave_table,to_regclass('public.ra_research_ledger') ledger_table")
            infra = cur.fetchone()
        conn.rollback()

    pti_source = None
    if reference_universe_id:
        try:
            pti_source = point_in_time_source_readiness(reference_universe_id, HISTORY_START_DATE, PRESEALED_END_DATE)
        except Exception as exc:
            pti_source = {"ready": False, "blockers": [f"Readiness audit failed: {exc}"]}

    months_available = len(_month_partitions(HISTORY_START_DATE, PRESEALED_END_DATE))
    return json_safe({
        "phase": "Phase 1 infrastructure",
        "execution_policy": "Full historical execution remains locked; one-day test only until point-in-time source readiness is green and explicitly enabled.",
        "historical_source_coverage": dict(inventory) if inventory else None,
        "point_in_time_source_readiness": pti_source,
        "configured_history_start": HISTORY_START_DATE,
        "presealed_end": PRESEALED_END_DATE,
        "months_available": months_available,
        "backfill": dict(backfill) if backfill else None,
        "months_completed": int(backfill["months_completed"]) if backfill else 0,
        "rows_processed": int(backfill["rows_processed"]) if backfill else 0,
        "current_processing_partition": dict(current_chunk) if current_chunk else None,
        "resume_retry_state": dict(job) if job else None,
        "market_state_feature_status": dict(market_state),
        "candidate_wave_infrastructure_status": dict(waves),
        "research_ledger_status": dict(ledger),
        "sealed_period_protection": {
            "enabled": bool(controls["sealed_guard_enabled"]) if controls else False,
            "sealed_start": controls["sealed_start_date"] if controls else SEALED_START_DATE,
            "full_history_execution_enabled": bool(controls["full_history_execution_enabled"]) if controls else False,
            "database_job_guard": True,
            "candidate_freeze_required": True,
        },
        "research_periods": [dict(x) for x in periods],
        "infrastructure": dict(infra),
        "feature_definition_hash": feature_definition_hash(),
        "feature_engine_version": FEATURE_VERSION,
        "latest_error": (job.get("error") if job else None) or (backfill.get("latest_error") if backfill else None),
    })
