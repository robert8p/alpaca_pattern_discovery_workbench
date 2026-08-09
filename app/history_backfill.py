from __future__ import annotations

import hashlib
import inspect
import random
import time as clock
from calendar import monthrange
from datetime import date, timedelta
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.features import (
    FEATURE_VERSION,
    _build_batch,
    _chunk_batches,
    _ensure_partitions_for_chunk,
    _feature_sql,
    _is_statement_timeout,
    _is_transient_database_conflict,
    _split_feature_batch,
    build_feature_set,
    date_chunks,
)
from app.jobs import JobInterrupted, add_event, check_control, set_progress
from app.models import HistoricalFeatureBackfillConfig, FeatureBuildConfig
from app.point_in_time_universe import ensure_point_in_time_universes, point_in_time_snapshot_for_date
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


def _ensure_backfill_record(
    job_id: str,
    config: HistoricalFeatureBackfillConfig,
    feature_config: FeatureBuildConfig,
    point_in_time_universe_run_id: str | None,
) -> str:
    source = {
        "timeframe": "1Min",
        "feed": "sip",
        "adjustment": "raw",
        "session": "regular",
        "universe_membership": "point_in_time_monthly" if point_in_time_universe_run_id else "legacy_static",
    }
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_full_history_backfills WHERE job_id=%s", (job_id,))
            row = cur.fetchone()
            if row:
                backfill_id = row["id"]
                cur.execute(
                    """
                    UPDATE ra_full_history_backfills SET status='running',latest_error=NULL,
                        point_in_time_universe_run_id=%s,started_at=COALESCE(started_at,now()) WHERE id=%s
                    """,
                    (point_in_time_universe_run_id, backfill_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO ra_full_history_backfills(
                        job_id,name,reference_feature_set_id,universe_run_id,scope,source_config,feature_config,
                        feature_definition_hash,requested_start,requested_end,status,months_available,started_at,
                        point_in_time_universe_run_id
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',%s,now(),%s) RETURNING id
                    """,
                    (
                        job_id,
                        config.name,
                        config.reference_feature_set_id,
                        feature_config.universe_run_id,
                        config.scope,
                        Jsonb(source),
                        Jsonb(feature_config.model_dump(mode="json")),
                        feature_definition_hash(),
                        config.start_date,
                        config.end_date,
                        len(_month_partitions(config.start_date, config.end_date)),
                        point_in_time_universe_run_id,
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
                        rows_processed=COALESCE((SELECT sum(c.rows_written) FROM ra_feature_chunks c WHERE c.feature_set_id=%s AND c.status='completed' AND c.chunk_start<=p.partition_end AND c.chunk_end>=p.partition_start),0),
                        point_in_time_snapshot_count=(SELECT count(DISTINCT m.point_in_time_snapshot_id) FROM ra_feature_chunks c JOIN ra_feature_chunk_universes m ON m.feature_chunk_id=c.id WHERE c.feature_set_id=%s AND c.chunk_start<=p.partition_end AND c.chunk_end>=p.partition_start)
                    WHERE p.backfill_id=%s
                    """,
                    (feature_set_id, feature_set_id, feature_set_id, feature_set_id, backfill_id),
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


def _ensure_master_feature_set(
    job_id: str,
    config: HistoricalFeatureBackfillConfig,
    base_config: FeatureBuildConfig,
    point_in_time_run_id: str,
) -> tuple[str, list[dict[str, Any]]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_feature_sets WHERE job_id=%s", (job_id,))
            existing = cur.fetchone()
            full_config = base_config.model_dump(mode="json")
            full_config["point_in_time_universe_run_id"] = point_in_time_run_id
            full_config["universe_membership_mode"] = "point_in_time_monthly"
            if existing:
                feature_set_id = existing["id"]
                cur.execute(
                    "UPDATE ra_feature_sets SET status='building',completed_at=NULL,config=%s,feature_version=%s WHERE id=%s",
                    (Jsonb(full_config), FEATURE_VERSION, feature_set_id),
                )
                cur.execute("UPDATE ra_feature_chunks SET status='pending',error=NULL WHERE feature_set_id=%s AND status IN ('running','failed','cancelled')", (feature_set_id,))
                cur.execute(
                    "UPDATE ra_feature_batches SET status='pending',error=NULL WHERE feature_chunk_id IN (SELECT id FROM ra_feature_chunks WHERE feature_set_id=%s) AND status IN ('running','failed','cancelled')",
                    (feature_set_id,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO ra_feature_sets(job_id,universe_run_id,name,config,feature_version,symbol_count,min_trade_date,max_trade_date)
                    VALUES (%s,%s,%s,%s,%s,0,%s,%s) RETURNING id
                    """,
                    (
                        job_id,
                        base_config.universe_run_id,
                        config.name,
                        Jsonb(full_config),
                        FEATURE_VERSION,
                        config.start_date,
                        config.end_date,
                    ),
                )
                feature_set_id = cur.fetchone()["id"]

            cur.execute(
                """
                SELECT * FROM ra_point_in_time_universe_snapshots
                WHERE point_in_time_universe_run_id=%s AND status='completed'
                  AND effective_end >= %s AND effective_start <= %s
                ORDER BY effective_start
                """,
                (point_in_time_run_id, config.start_date, config.end_date),
            )
            snapshots = [dict(row) for row in cur.fetchall()]
            for snapshot in snapshots:
                seg_start = max(config.start_date, snapshot["effective_start"])
                seg_end = min(config.end_date, snapshot["effective_end"])
                for chunk_start, chunk_end in date_chunks(seg_start, seg_end, base_config.date_chunk_days):
                    cur.execute(
                        """
                        INSERT INTO ra_feature_chunks(feature_set_id,chunk_start,chunk_end)
                        VALUES (%s,%s,%s) ON CONFLICT DO NOTHING
                        """,
                        (feature_set_id, chunk_start, chunk_end),
                    )
                    cur.execute(
                        "SELECT id FROM ra_feature_chunks WHERE feature_set_id=%s AND chunk_start=%s AND chunk_end=%s",
                        (feature_set_id, chunk_start, chunk_end),
                    )
                    chunk_id = cur.fetchone()["id"]
                    if snapshot.get("snapshot_universe_run_id"):
                        cur.execute(
                            """
                            INSERT INTO ra_feature_chunk_universes(feature_chunk_id,point_in_time_snapshot_id,universe_run_id)
                            VALUES (%s,%s,%s) ON CONFLICT (feature_chunk_id) DO UPDATE SET
                                point_in_time_snapshot_id=excluded.point_in_time_snapshot_id,
                                universe_run_id=excluded.universe_run_id
                            """,
                            (chunk_id, snapshot["id"], snapshot["snapshot_universe_run_id"]),
                        )
            cur.execute("SELECT * FROM ra_feature_chunks WHERE feature_set_id=%s ORDER BY chunk_start", (feature_set_id,))
            chunks = [dict(row) for row in cur.fetchall()]
        conn.commit()
    return str(feature_set_id), chunks


def _ensure_point_in_time_batches(chunks: list[dict[str, Any]], base_config: FeatureBuildConfig) -> None:
    with connection() as conn:
        with conn.cursor() as cur:
            for chunk in chunks:
                if chunk["status"] == "completed":
                    continue
                cur.execute("SELECT count(*) AS batches FROM ra_feature_batches WHERE feature_chunk_id=%s", (chunk["id"],))
                if int(cur.fetchone()["batches"]):
                    continue
                cur.execute("SELECT universe_run_id FROM ra_feature_chunk_universes WHERE feature_chunk_id=%s", (chunk["id"],))
                mapping = cur.fetchone()
                if not mapping:
                    raise RuntimeError(f"Feature chunk {chunk['id']} is missing point-in-time universe provenance")
                cur.execute(
                    """
                    SELECT symbol FROM ra_analysis_universe
                    WHERE universe_run_id=%s AND included AND liquidity_tier=ANY(%s)
                    ORDER BY rank_by_liquidity NULLS LAST,symbol
                    """,
                    (mapping["universe_run_id"], base_config.liquidity_tiers),
                )
                symbols = [row["symbol"] for row in cur.fetchall()]
                if symbols:
                    batches = [symbols[i:i + base_config.symbol_batch_size] for i in range(0, len(symbols), base_config.symbol_batch_size)]
                    cur.executemany(
                        "INSERT INTO ra_feature_batches(feature_chunk_id,batch_number,symbols) VALUES (%s,%s,%s)",
                        [(chunk["id"], i, symbols_batch) for i, symbols_batch in enumerate(batches, start=1)],
                    )
        conn.commit()


def _chunk_feature_config(chunk: dict[str, Any], base_config: FeatureBuildConfig) -> FeatureBuildConfig:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT universe_run_id FROM ra_feature_chunk_universes WHERE feature_chunk_id=%s", (chunk["id"],))
            mapping = cur.fetchone()
        conn.rollback()
    if not mapping:
        raise RuntimeError(f"Feature chunk {chunk['id']} has no point-in-time universe mapping")
    return base_config.model_copy(update={
        "universe_run_id": mapping["universe_run_id"],
        "start_date": chunk["chunk_start"],
        "end_date": chunk["chunk_end"],
    })


def _run_point_in_time_feature_set(
    job_id: str,
    config: HistoricalFeatureBackfillConfig,
    base_config: FeatureBuildConfig,
    point_in_time_run_id: str,
) -> dict[str, Any]:
    feature_set_id, chunks = _ensure_master_feature_set(job_id, config, base_config, point_in_time_run_id)
    _ensure_point_in_time_batches(chunks, base_config)
    total = len(chunks)
    completed = sum(1 for chunk in chunks if chunk["status"] == "completed")
    set_progress(job_id, "building point-in-time feature chunks", completed, total, result={"feature_set_id": feature_set_id})
    add_event(
        job_id,
        "point_in_time_feature_set_ready",
        f"Historical feature set planned in {total} date chunks using point-in-time universe snapshots.",
        details={"feature_set_id": feature_set_id, "point_in_time_universe_run_id": point_in_time_run_id},
    )

    for chunk_number, chunk in enumerate(chunks, start=1):
        if chunk["status"] == "completed":
            continue
        check_control(job_id)
        _ensure_partitions_for_chunk(chunk)
        chunk_config = _chunk_feature_config(chunk, base_config)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE ra_feature_chunks SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s",
                    (chunk["id"],),
                )
            conn.commit()
        try:
            while True:
                batches = _chunk_batches(chunk["id"])
                pending = [(index, batch) for index, batch in enumerate(batches, start=1) if batch["status"] != "completed"]
                if not pending:
                    break
                batch_index, batch = pending[0]
                check_control(job_id)
                set_progress(
                    job_id,
                    f"PTI chunk {chunk_number}/{total} · symbols {batch_index}/{len(batches)} · {len(batch['symbols'])} tickers · {chunk['chunk_start']} to {chunk['chunk_end']}",
                    completed,
                    total,
                )
                with connection() as conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE ra_feature_batches SET status='running',attempts=attempts+1,started_at=COALESCE(started_at,now()),error=NULL WHERE id=%s",
                            (batch["id"],),
                        )
                    conn.commit()
                try:
                    rows = _build_batch(job_id, feature_set_id, chunk, batch, chunk_config)
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute(
                                "UPDATE ra_feature_batches SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s",
                                (rows, batch["id"]),
                            )
                        conn.commit()
                except JobInterrupted:
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE ra_feature_batches SET status='pending',error=NULL WHERE id=%s", (batch["id"],))
                        conn.commit()
                    raise
                except Exception as exc:
                    current_attempt = int(batch.get("attempts") or 0) + 1
                    if _is_transient_database_conflict(exc) and current_attempt <= get_settings().feature_db_conflict_retries:
                        delay = min(30.0, (2 ** max(0, current_attempt - 1)) + random.uniform(0.25, 1.25))
                        with connection() as conn:
                            with conn.cursor() as cur:
                                cur.execute("UPDATE ra_feature_batches SET status='pending',error=%s WHERE id=%s", (f"Transient database conflict; retrying: {exc}", batch["id"]))
                            conn.commit()
                        clock.sleep(delay)
                        continue
                    if _is_statement_timeout(exc) and _split_feature_batch(job_id, batch):
                        continue
                    with connection() as conn:
                        with conn.cursor() as cur:
                            cur.execute("UPDATE ra_feature_batches SET status='failed',error=%s WHERE id=%s", (str(exc), batch["id"]))
                        conn.commit()
                    raise

            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT COALESCE(sum(rows_written),0) AS rows FROM ra_feature_batches WHERE feature_chunk_id=%s", (chunk["id"],))
                    rows = int(cur.fetchone()["rows"])
                    cur.execute("UPDATE ra_feature_chunks SET status='completed',rows_written=%s,completed_at=now(),error=NULL WHERE id=%s", (rows, chunk["id"]))
                conn.commit()
            completed += 1
            set_progress(job_id, f"completed PTI {chunk['chunk_start']} to {chunk['chunk_end']}", completed, total)
            add_event(job_id, "feature_chunk_completed", f"Built {rows:,} point-in-time feature rows for {chunk['chunk_start']} to {chunk['chunk_end']}.")
        except JobInterrupted:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_feature_chunks SET status='pending',error=NULL WHERE id=%s", (chunk["id"],))
                    cur.execute("UPDATE ra_feature_sets SET status='building' WHERE id=%s", (feature_set_id,))
                conn.commit()
            raise
        except Exception as exc:
            with connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("UPDATE ra_feature_chunks SET status='failed',error=%s WHERE id=%s", (str(exc), chunk["id"]))
                    cur.execute("UPDATE ra_feature_sets SET status='failed' WHERE id=%s", (feature_set_id,))
                conn.commit()
            raise

    check_control(job_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) AS rows,count(DISTINCT symbol) AS symbols,min(trade_date) AS first_date,max(trade_date) AS last_date FROM ra_intraday_features WHERE feature_set_id=%s",
                (feature_set_id,),
            )
            summary = cur.fetchone()
            cur.execute(
                """
                UPDATE ra_feature_sets SET status='completed',row_count=%s,symbol_count=%s,
                    min_trade_date=%s,max_trade_date=%s,completed_at=now() WHERE id=%s
                """,
                (summary["rows"], summary["symbols"], summary["first_date"], summary["last_date"], feature_set_id),
            )
        conn.commit()
    result = {"feature_set_id": feature_set_id, **dict(summary), "point_in_time_universe_run_id": point_in_time_run_id}
    add_event(job_id, "feature_set_completed", f"Point-in-time feature set contains {summary['rows']:,} rows across {summary['symbols']:,} symbols.", details=result)
    return json_safe(result)


def run_historical_feature_backfill(job_id: str, config: HistoricalFeatureBackfillConfig) -> dict[str, Any]:
    base_config = backfill_feature_config(config)
    _, reference_universe_run_id = _reference_feature_config(config.reference_feature_set_id)

    # Build point-in-time universe snapshots first. Each snapshot uses the exact
    # existing universe methodology, but its source data ends on T-1.
    pti = ensure_point_in_time_universes(job_id, config, reference_universe_run_id)
    point_in_time_run_id = str(pti["point_in_time_universe_run_id"])
    backfill_id = _ensure_backfill_record(job_id, config, base_config, point_in_time_run_id)

    add_event(
        job_id,
        "historical_backfill_started",
        "Historical feature backfill uses the unchanged engineered-feature pipeline with point-in-time universe membership.",
        details={
            "backfill_id": backfill_id,
            "feature_definition_hash": feature_definition_hash(),
            "scope": config.scope,
            "point_in_time_universe_run_id": point_in_time_run_id,
        },
    )
    try:
        if config.scope == "one_day_test":
            snapshot = point_in_time_snapshot_for_date(point_in_time_run_id, config.start_date)
            if snapshot and int(snapshot.get("included_symbols") or 0) > 0:
                one_day_config = base_config.model_copy(update={"universe_run_id": snapshot["snapshot_universe_run_id"]})
                result = build_feature_set(job_id, one_day_config)
                result["point_in_time_universe_run_id"] = point_in_time_run_id
                result["point_in_time_snapshot_id"] = str(snapshot["id"])
                result["universe_membership_mode"] = "point_in_time"
            else:
                # Very early history can predate the minimum 15 trading-day
                # liquidity requirement. The legacy fallback exists only for the
                # deliberately scoped one-day plumbing test and is explicitly
                # recorded; full-history execution never uses this fallback.
                add_event(
                    job_id,
                    "point_in_time_universe_insufficient_history",
                    "The test date has no eligible point-in-time universe because the historical source lacks the required prior trading days; using the legacy static universe for plumbing only.",
                    level="warning",
                    details={"test_date": config.start_date.isoformat()},
                )
                result = build_feature_set(job_id, base_config)
                result["point_in_time_universe_run_id"] = point_in_time_run_id
                result["universe_membership_mode"] = "legacy_static_test_fallback"
        else:
            result = _run_point_in_time_feature_set(job_id, config, base_config, point_in_time_run_id)

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
