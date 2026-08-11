from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.db import connection
from app.jobs import add_event, check_control
from app.models import HistoricalFeatureBackfillConfig, UniverseBuildConfig
from app.universe import build_universe
from app.utils import json_safe

PTI_UNIVERSE_VERSION = "1.0.1"
PTI_LOOKBACK_CALENDAR_DAYS = 61
PTI_SCHEMA_LOCK = "alpaca_pattern_discovery_pti_universe_schema"


def ensure_point_in_time_schema() -> None:
    """Install the additive point-in-time schema if startup predates this module.

    Phase 1 v2.5.0 shipped before the point-in-time universe layer. The live
    project receives this migration directly, while this idempotent lazy path
    keeps fresh CI databases and disaster-recovery installs safe as well.
    """
    migration = Path(__file__).resolve().parent.parent / "sql" / "migrations" / "2.6.0.sql"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (PTI_SCHEMA_LOCK,))
            cur.execute("SELECT to_regclass('public.ra_point_in_time_universe_runs') AS t")
            if not cur.fetchone()["t"]:
                cur.execute(migration.read_text(encoding="utf-8"))
        conn.commit()


def _reference_universe(reference_universe_run_id: UUID | str) -> dict[str, Any]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id,name,source_config,selection_config FROM ra_universe_runs WHERE id=%s",
                (reference_universe_run_id,),
            )
            row = cur.fetchone()
        conn.rollback()
    if not row:
        raise ValueError("Reference universe run does not exist")
    return dict(row)


def snapshot_universe_config(reference_universe_run_id: UUID | str, snapshot_date: date) -> UniverseBuildConfig:
    """Clone the existing liquid-universe methodology using past data only."""
    ref = _reference_universe(reference_universe_run_id)
    selection = dict(ref.get("selection_config") or {})
    # 61 calendar days mirrors the original June-04 through Aug-03 inclusive
    # universe window. The snapshot ends on T-1, never on T.
    lookback_end = snapshot_date - timedelta(days=1)
    lookback_start = snapshot_date - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS)
    selection.update(
        {
            "name": f"PTI liquid universe · {snapshot_date.isoformat()}",
            "start_date": lookback_start,
            "end_date": lookback_end,
        }
    )
    return UniverseBuildConfig.model_validate(selection)


def _ensure_pti_run(job_id: str, config: HistoricalFeatureBackfillConfig, reference_universe_run_id: UUID | str) -> str:
    ensure_point_in_time_schema()
    ref = _reference_universe(reference_universe_run_id)
    cadence = "single_date" if config.scope == "one_day_test" else "monthly"
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_point_in_time_universe_runs WHERE parent_job_id=%s", (job_id,))
            existing = cur.fetchone()
            if existing:
                run_id = existing["id"]
                cur.execute(
                    "UPDATE ra_point_in_time_universe_runs SET status='running',latest_error=NULL,started_at=COALESCE(started_at,now()) WHERE id=%s",
                    (run_id,),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO ra_point_in_time_universe_runs(
                        parent_job_id,reference_universe_run_id,name,requested_start,requested_end,cadence,
                        lookback_calendar_days,source_config,selection_config,methodology_version,status,started_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'running',now()) RETURNING id
                    """,
                    (
                        job_id,
                        reference_universe_run_id,
                        f"Point-in-time universe · {config.name}",
                        config.start_date,
                        config.end_date,
                        cadence,
                        PTI_LOOKBACK_CALENDAR_DAYS,
                        Jsonb(ref.get("source_config") or {}),
                        Jsonb(ref.get("selection_config") or {}),
                        PTI_UNIVERSE_VERSION,
                    ),
                )
                run_id = cur.fetchone()["id"]
        conn.commit()
    return str(run_id)


def _snapshot_dates(config: HistoricalFeatureBackfillConfig, reference_universe_run_id: UUID | str) -> list[date]:
    if config.scope == "one_day_test":
        return [config.start_date]
    ref = _reference_universe(reference_universe_run_id)
    source = dict(ref.get("source_config") or {})
    selection = dict(ref.get("selection_config") or {})
    minimum_trading_days = int(selection.get("minimum_trading_days") or 15)
    with connection() as conn:
        with conn.cursor() as cur:
            # Use SPY only as a trading-calendar proxy. Eligibility still comes
            # from each stock's own bars inside build_universe(). Crucially, we
            # count warm-up dates only from the requested broad-history start,
            # not SPY's older standalone history.
            cur.execute(
                """
                SELECT DISTINCT (b.bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date
                FROM rd_bars b
                WHERE b.symbol='SPY' AND b.timeframe=%s AND b.feed=%s AND b.adjustment=%s
                  AND b.session_label='regular'
                  AND (b.bar_ts AT TIME ZONE 'America/New_York')::date BETWEEN %s AND %s
                ORDER BY trade_date
                """,
                (
                    source.get("timeframe", "1Min"),
                    source.get("feed", "sip"),
                    source.get("adjustment", "raw"),
                    config.start_date,
                    config.end_date,
                ),
            )
            trading_dates = [row["trade_date"] for row in cur.fetchall()]
        conn.rollback()
    if not trading_dates:
        return []

    eligible_dates: list[date] = []
    for candidate in trading_dates:
        prior_count = sum(
            1
            for prior in trading_dates
            if candidate - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS) <= prior < candidate
        )
        if prior_count >= minimum_trading_days:
            eligible_dates.append(candidate)
    if not eligible_dates:
        return []

    # The first eligible date is an explicit bootstrap snapshot so late-May
    # history is not silently discarded just because the monthly snapshot at
    # the start of May lacked the required 15 prior trading days. Thereafter we
    # use the first eligible trading date of each month.
    snapshots = [eligible_dates[0]]
    seen_months = {(eligible_dates[0].year, eligible_dates[0].month)}
    for d in eligible_dates[1:]:
        key = (d.year, d.month)
        if key not in seen_months:
            snapshots.append(d)
            seen_months.add(key)
    return snapshots


def _plan_snapshots(run_id: str, config: HistoricalFeatureBackfillConfig, reference_universe_run_id: UUID | str) -> None:
    dates = _snapshot_dates(config, reference_universe_run_id)
    with connection() as conn:
        with conn.cursor() as cur:
            for index, snapshot_date in enumerate(dates):
                next_date = dates[index + 1] if index + 1 < len(dates) else config.end_date + timedelta(days=1)
                effective_start = max(config.start_date, snapshot_date)
                effective_end = min(config.end_date, next_date - timedelta(days=1))
                cur.execute(
                    """
                    INSERT INTO ra_point_in_time_universe_snapshots(
                        point_in_time_universe_run_id,snapshot_date,effective_start,effective_end,lookback_start,lookback_end
                    ) VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (point_in_time_universe_run_id,snapshot_date) DO NOTHING
                    """,
                    (
                        run_id,
                        snapshot_date,
                        effective_start,
                        effective_end,
                        snapshot_date - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS),
                        snapshot_date - timedelta(days=1),
                    ),
                )
            cur.execute(
                "UPDATE ra_point_in_time_universe_runs SET snapshots_total=(SELECT count(*) FROM ra_point_in_time_universe_snapshots WHERE point_in_time_universe_run_id=%s) WHERE id=%s",
                (run_id, run_id),
            )
        conn.commit()


def _child_job(snapshot: dict[str, Any], cfg: UniverseBuildConfig) -> str:
    child_job_id = snapshot.get("child_job_id")
    with connection() as conn:
        with conn.cursor() as cur:
            if child_job_id:
                cur.execute(
                    """
                    UPDATE ra_jobs SET status='running',phase='point-in-time universe snapshot',
                        config=%s,error=NULL,completed_at=NULL,started_at=COALESCE(started_at,now())
                    WHERE id=%s
                    """,
                    (Jsonb(cfg.model_dump(mode="json")), child_job_id),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO ra_jobs(job_type,name,status,phase,config,started_at)
                    VALUES ('universe_build',%s,'running','point-in-time universe snapshot',%s,now()) RETURNING id
                    """,
                    (cfg.name, Jsonb(cfg.model_dump(mode="json"))),
                )
                child_job_id = cur.fetchone()["id"]
                cur.execute(
                    "UPDATE ra_point_in_time_universe_snapshots SET child_job_id=%s WHERE id=%s",
                    (child_job_id, snapshot["id"]),
                )
        conn.commit()
    return str(child_job_id)


def _build_snapshot(parent_job_id: str, snapshot: dict[str, Any], reference_universe_run_id: UUID | str) -> dict[str, Any]:
    if snapshot["status"] == "completed" and snapshot.get("snapshot_universe_run_id"):
        return snapshot
    check_control(parent_job_id)
    cfg = snapshot_universe_config(reference_universe_run_id, snapshot["snapshot_date"])
    child_job_id = _child_job(snapshot, cfg)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE ra_point_in_time_universe_snapshots SET status='running',attempts=attempts+1,error=NULL,started_at=COALESCE(started_at,now()) WHERE id=%s",
                (snapshot["id"],),
            )
        conn.commit()
    try:
        result = build_universe(child_job_id, cfg)
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ra_point_in_time_universe_snapshots SET status='completed',snapshot_universe_run_id=%s,
                        total_symbols=%s,included_symbols=%s,tier_a_symbols=%s,tier_b_symbols=%s,
                        tier_c_symbols=%s,tier_d_symbols=%s,completed_at=now(),error=NULL
                    WHERE id=%s
                    """,
                    (
                        result["universe_run_id"], result["total_symbols"], result["included_symbols"],
                        result["tier_a_symbols"], result["tier_b_symbols"], result["tier_c_symbols"], result["tier_d_symbols"],
                        snapshot["id"],
                    ),
                )
                cur.execute(
                    "UPDATE ra_jobs SET status='completed',phase='completed',result=%s,progress_current=4,progress_total=4,completed_at=now(),heartbeat_at=now() WHERE id=%s",
                    (Jsonb(result), child_job_id),
                )
            conn.commit()
    except Exception as exc:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_point_in_time_universe_snapshots SET status='failed',error=%s WHERE id=%s", (str(exc), snapshot["id"]))
                cur.execute("UPDATE ra_jobs SET status='failed',error=%s,completed_at=now() WHERE id=%s", (str(exc), child_job_id))
            conn.commit()
        raise
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ra_point_in_time_universe_snapshots WHERE id=%s", (snapshot["id"],))
            updated = dict(cur.fetchone())
        conn.rollback()
    add_event(
        parent_job_id,
        "point_in_time_universe_snapshot_completed",
        f"Point-in-time universe for {updated['snapshot_date']} contains {updated['included_symbols']:,} included symbols using data through {updated['lookback_end']}.",
        details={
            "snapshot_id": str(updated["id"]),
            "snapshot_universe_run_id": str(updated["snapshot_universe_run_id"]),
            "included_symbols": updated["included_symbols"],
            "lookback_end": updated["lookback_end"],
        },
    )
    return updated


def ensure_point_in_time_universes(
    job_id: str,
    config: HistoricalFeatureBackfillConfig,
    reference_universe_run_id: UUID | str,
) -> dict[str, Any]:
    """Create immutable-as-of monthly universe snapshots before feature work.

    Each snapshot invokes the exact existing UniverseBuildConfig methodology but
    sets its source window to end on T-1. The first snapshot is the earliest date
    with the required historical liquidity warm-up; monthly snapshots follow.
    """
    run_id = _ensure_pti_run(job_id, config, reference_universe_run_id)
    _plan_snapshots(run_id, config, reference_universe_run_id)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ra_point_in_time_universe_snapshots WHERE point_in_time_universe_run_id=%s ORDER BY snapshot_date",
                (run_id,),
            )
            snapshots = [dict(row) for row in cur.fetchall()]
        conn.rollback()
    built: list[dict[str, Any]] = []
    try:
        for snapshot in snapshots:
            built.append(_build_snapshot(job_id, snapshot, reference_universe_run_id))
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE ra_point_in_time_universe_runs SET status='completed',
                        snapshots_completed=(SELECT count(*) FROM ra_point_in_time_universe_snapshots WHERE point_in_time_universe_run_id=%s AND status='completed'),
                        earliest_usable_date=(SELECT min(effective_start) FROM ra_point_in_time_universe_snapshots WHERE point_in_time_universe_run_id=%s AND status='completed' AND included_symbols>0),
                        latest_snapshot_date=(SELECT max(snapshot_date) FROM ra_point_in_time_universe_snapshots WHERE point_in_time_universe_run_id=%s AND status='completed'),
                        completed_at=now(),latest_error=NULL
                    WHERE id=%s
                    """,
                    (run_id, run_id, run_id, run_id),
                )
            conn.commit()
    except Exception as exc:
        with connection() as conn:
            with conn.cursor() as cur:
                cur.execute("UPDATE ra_point_in_time_universe_runs SET status='failed',latest_error=%s WHERE id=%s", (str(exc), run_id))
            conn.commit()
        raise
    return json_safe({"point_in_time_universe_run_id": run_id, "snapshots": built, "version": PTI_UNIVERSE_VERSION})


def point_in_time_snapshot_for_date(run_id: UUID | str, trade_date: date) -> dict[str, Any] | None:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM ra_point_in_time_universe_snapshots
                WHERE point_in_time_universe_run_id=%s AND status='completed'
                  AND %s BETWEEN effective_start AND effective_end
                ORDER BY snapshot_date DESC LIMIT 1
                """,
                (run_id, trade_date),
            )
            row = cur.fetchone()
        conn.rollback()
    return dict(row) if row else None
