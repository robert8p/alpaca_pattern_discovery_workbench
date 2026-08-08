from __future__ import annotations

import hashlib
import inspect
from calendar import monthrange
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import connection
from app.features import _feature_sql, build_feature_set
from app.jobs import add_event
from app.models import HistoricalFeatureBackfillConfig, FeatureBuildConfig
from app.research_policy import PRESEALED_END_DATE
from app.utils import json_safe

def feature_definition_hash() -> str:
    """Hash the unchanged engineered-feature SQL generator used by normal feature builds."""
    source = inspect.getsource(_feature_sql).encode("utf-8")
    return hashlib.sha256(source).hexdigest()


def _month_partitions(start: date, end: date) -> list[tuple[date, date]]:
    parts: list[tuple[date, date]] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        last = date(cursor.year, cursor.month, monthrange(cursor.year, cursor.month)[1])
        parts.append((max(start, cursor), min(end, last)))
        cursor = (last + timedelta(days=1)).replace(day=1)
    return parts


def _stage_for_period(start: date, end: date) -> str | None:
    periods = [
        ("discovery", date(2025, 5, 4), date(2026, 2, 28)),
        ("validation", date(2026, 3, 1), date(2026, 5, 31)),
        ("research_confirmation", date(2026, 6, 1), PRESEALED_END_DATE),
    ]
    for stage, pstart, pend in periods:
        if start >= pstart and end <= pend:
            return stage
    return None


def _reference_feature_config(reference_feature_set_id: UUID) -> tuple[dict[str, Any], UUID]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT config,universe_run_id,status FROM ra_feature_sets WHERE id=%s", (reference_feature_set_id,))
            row = cur.fetchone()
        conn.rollback()
    if not row or row["status"] != "completed":
        raise ValueError("Reference feature set must be completed")
    return dict(row["config"] or {}), row["universe_run_id"]


def backfill_feature_config(config: HistoricalFeatureBackfillConfig) -> FeatureBuildConfig:
    ref, universe_run_id = _reference_feature_config(config.reference_feature_set_id)
    expected = {"timeframe": "1Min", "feed": "sip", "adjustment": "raw", "session": "regular"}
    for key, value in expected.items():
        if ref.get(key) != value:
            raise ValueError(f"Reference feature set is not {key}={value}")
    return FeatureBuildConfig.model_validate({
        "name": config.name,
        "universe_run_id": universe_run_id,
        "start_date": config.start_date,
        "end_date": config.end_date,
        "timeframe": "1Min",
        "feed": "sip",
        "adjustment": "raw",
        "session": "regular",
        "liquidity_tiers": ref.get("liquidity_tiers") or ["A", "B", "C", "D"],
        "date_chunk_days": 3,
        "symbol_batch_size": int(ref.get("symbol_batch_size") or 100),
        "time_of_day_baseline_days": int(ref.get("time_of_day_baseline_days") or 10),
        "predictor_horizons_minutes": ref.get("predictor_horizons_minutes") or [1, 5, 15, 30, 60],
        "outcome_horizons_minutes": ref.get("outcome_horizons_minutes") or [5, 15, 30, 60],
        "conflict_policy": "skip_existing",
    })


def _ensure_backfill_record(job_id: str, config: HistoricalFeatureBackfillConfig, feature_config: FeatureBuildConfig) -> str:
    source = {"timeframe": "1Min", "feed": "sip", "adjustment": "raw", "session": "regular"}
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_full_history_backfills WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row:
                backfill_id = row["id"]
                cur.execute("UPDATE ra_full_history_backfills SET status='running',latest_error=NULL,started_at=COALESCE(started_at,now()) WHERE id=%s", (backfill_id,))
            else:
                cur.execute(
                    """
                    INSERT INTO ra_full_history_backfills(
                        job_id,name,reference_feature_set_id,universe_run_id,scope,source_config,feature_config,
                        feature_definition_hash,requested_start,requested_end,status,months_available,started_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',%s,now()) RETURNING id
                    """,
                    (
                        job_id, config.name, config.reference_feature_set_id, feature_config.universe_run_id, config.scope,
                        Jsonb(source), Jsonb(feature_config.model_dump(mode="json")), feature_definition_hash(),
                        config.start_date, config.end_date, len(_month_partitions(config.start_date, config.end_date)),
                    ),
                )
                backfill_id = cur.fetchone()["id"]
                for pstart, pend in _month_partitions(config.start_date, config.end_date):
                    cur.execute(
                        """
                        INSERT INTO ra_full_history_backfill_partitions(backfill_id,partition_start,partition_end,research_stage)
                        VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING
                        """,
                        (backfill_id, pstart, pend, _stage_for_period(pstart, pend)),
                    )
        conn.commit()
    return str(backfill_id)


def _sync_backfill_status(backfill_id: str, job_id: str, error: str | None = None) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_feature_sets WHERE job_id=%s", (job_id,))
            fs = cur.fetchone()
            feature_set_id = fs["id"] if fs else None
            if feature_set_id:
                cur.execute(
                    """
                    UPDATE ra_full_history_backfill_partitions p SET
                        feature_chunks_total=(SELECT count(*) FROM ra_feature_chunks c WHERE c.feature_set_id=%s AND c.chunk_start<=p.partition_end AND c.chunk_end>=p.partition_start),
                        feature_chunks_completed=(SELECT count(*) FROM ra_feature_chunks c WHERE c.feature_set_id=%s AND c.status='completed' AND c.chunk_start<=p.partition_end AND c.chunk_end>=p.partition_start),
                        rows_processed=COALESCE((SELECT sum(c.rows_written) FROM ra_feature_chunks c WHERE c.feature_set_id=%s AND c.status='completed' AND c.chunk_start<=p.partition_end AND c.chunk_end>=p.partition_start),0)
                    WHERE p.backfill_id=%s
                    """,
                    (feature_set_id, feature_set_id, feature_set_id, backfill_id),
                )
                cur.execute(
                    """
                    UPDATE ra_full_history_backfill_partitions SET status='completed',completed_at=COALESCE(completed_at,now())
                    WHERE backfill_id=%s AND feature_chunks_total>0 AND feature_chunks_completed=feature_chunks_total
                    """,
                    (backfill_id,),
                )
            cur.execute(
                """
                UPDATE ra_full_history_backfills b SET feature_set_id=%s,
                    months_completed=(SELECT count(*) FROM ra_full_history_backfill_partitions p WHERE p.backfill_id=b.id AND p.status='completed'),
                    rows_processed=COALESCE((SELECT sum(c.rows_written) FROM ra_feature_chunks c WHERE c.feature_set_id=%s AND c.status='completed'),0),
                    latest_error=%s
                WHERE b.id=%s
                """,
                (feature_set_id, feature_set_id, error, backfill_id),
            )
        conn.commit()


def run_historical_feature_backfill(job_id: str, config: HistoricalFeatureBackfillConfig) -> dict[str, Any]:
    feature_config = backfill_feature_config(config)
    backfill_id = _ensure_backfill_record(job_id, config, feature_config)
    add_event(job_id, "historical_backfill_started", "Historical feature backfill uses the unchanged engineered-feature pipeline.", details={
        "backfill_id": backfill_id, "feature_definition_hash": feature_definition_hash(), "scope": config.scope,
    })
    try:
        result = build_feature_set(job_id, feature_config)
        _sync_backfill_status(backfill_id, job_id)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_full_history_backfills SET status='completed',completed_at=now(),current_partition=NULL WHERE id=%s", (backfill_id,))
            conn.commit()
        return json_safe({"backfill_id": backfill_id, **result})
    except Exception as exc:
        _sync_backfill_status(backfill_id, job_id, str(exc))
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_full_history_backfills SET status='failed',latest_error=%s WHERE id=%s", (str(exc), backfill_id))
            conn.commit()
        raise


