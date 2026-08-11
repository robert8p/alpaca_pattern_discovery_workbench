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

PTI_UNIVERSE_VERSION = "1.1.0"
PTI_LOOKBACK_CALENDAR_DAYS = 61
PTI_SCHEMA_LOCK = "alpaca_pattern_discovery_pti_universe_schema"


def ensure_point_in_time_schema() -> None:
    """Install the additive point-in-time schema if startup predates this module."""
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


def _covers_range(intervals: list[tuple[date, date]], required_start: date, required_end: date) -> bool:
    """Return true when completed source-job intervals continuously cover the required calendar range."""
    cursor = required_start
    for start, end in sorted(intervals):
        if end < cursor:
            continue
        if start > cursor:
            return False
        cursor = max(cursor, end + timedelta(days=1))
        if cursor > required_end:
            return True
    return cursor > required_end


def _source_job_intervals(mode: str, source: dict[str, Any]) -> list[dict[str, Any]]:
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,name,(config->>'start_date')::date AS start_date,(config->>'end_date')::date AS end_date,
                       symbol_count,total_tasks,completed_tasks
                FROM rd_jobs
                WHERE status='completed'
                  AND config->'timeframes' @> '["1Min"]'::jsonb
                  AND config->>'feed'=%s AND config->>'adjustment'=%s
                  AND config->'session'->>'mode'='all'
                  AND config->'universe'->>'mode'=%s
                ORDER BY (config->>'start_date')::date,created_at
                """,
                (source.get("feed", "sip"), source.get("adjustment", "raw"), mode),
            )
            rows = [dict(row) for row in cur.fetchall()]
        conn.rollback()
    return rows


def _spy_trading_dates(source: dict[str, Any], start_date: date, end_date: date) -> list[date]:
    with connection() as conn:
        with conn.cursor() as cur:
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
                    start_date,
                    end_date,
                ),
            )
            dates = [row["trade_date"] for row in cur.fetchall()]
        conn.rollback()
    return dates


def point_in_time_source_readiness(
    reference_universe_run_id: UUID | str,
    research_start: date,
    research_end: date,
) -> dict[str, Any]:
    """Audit whether raw history is sufficient for a survivorship-aware 61-day PTI universe.

    The existing broad all-active history supplies securities still active later.
    Two explicit supplements are required before full-history research is allowed:
    1) all-known history for the entire 61-calendar-day lookback before the first
       research trading day; and
    2) inactive-known history across the research period, so later-delisted names
       are not absent merely because they are inactive now.
    """
    ref = _reference_universe(reference_universe_run_id)
    source = dict(ref.get("source_config") or {})
    calendar_start = research_start - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS + 7)
    trading_dates = _spy_trading_dates(source, calendar_start, research_end)
    research_dates = [d for d in trading_dates if research_start <= d <= research_end]
    blockers: list[str] = []
    if not research_dates:
        return {
            "ready": False,
            "blockers": ["No SPY regular-session trading dates are available inside the requested research period."],
            "methodology_version": PTI_UNIVERSE_VERSION,
        }

    first_trade = research_dates[0]
    last_trade = research_dates[-1]
    warmup_start = first_trade - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS)
    prior_trading_dates = [d for d in trading_dates if warmup_start <= d < first_trade]
    warmup_end = prior_trading_dates[-1] if prior_trading_dates else first_trade - timedelta(days=1)

    selection = dict(ref.get("selection_config") or {})
    minimum_trading_days = int(selection.get("minimum_trading_days") or 15)
    if len(prior_trading_dates) < minimum_trading_days:
        blockers.append(
            f"Only {len(prior_trading_dates)} SPY trading dates exist in the 61-day pre-research window; {minimum_trading_days} are required."
        )

    active_jobs = _source_job_intervals("all_active", source)
    warmup_jobs = _source_job_intervals("all_known", source)
    inactive_jobs = _source_job_intervals("inactive_known", source)

    active_intervals = [(row["start_date"], row["end_date"]) for row in active_jobs]
    warmup_intervals = [(row["start_date"], row["end_date"]) for row in warmup_jobs]
    inactive_intervals = [(row["start_date"], row["end_date"]) for row in inactive_jobs]

    active_ready = _covers_range(active_intervals, first_trade, last_trade)
    warmup_ready = _covers_range(warmup_intervals, warmup_start, warmup_end)
    inactive_ready = _covers_range(inactive_intervals, first_trade, last_trade)

    if not active_ready:
        blockers.append(f"Completed all-active 1Min SIP/raw/all-session source jobs do not cover {first_trade} through {last_trade}.")
    if not warmup_ready:
        blockers.append(f"Completed all-known warm-up jobs do not cover the full 61-day universe lookback {warmup_start} through {warmup_end}.")
    if not inactive_ready:
        blockers.append(f"Completed inactive-known survivorship supplement does not cover {first_trade} through {last_trade}.")

    return {
        "ready": not blockers,
        "blockers": blockers,
        "methodology_version": PTI_UNIVERSE_VERSION,
        "research_start": research_start,
        "research_end": research_end,
        "first_research_trade_date": first_trade,
        "last_research_trade_date": last_trade,
        "required_warmup_start": warmup_start,
        "required_warmup_end": warmup_end,
        "prior_trading_dates": len(prior_trading_dates),
        "minimum_trading_days": minimum_trading_days,
        "active_history_ready": active_ready,
        "all_known_warmup_ready": warmup_ready,
        "inactive_survivorship_ready": inactive_ready,
        "active_source_job_ids": [str(row["id"]) for row in active_jobs],
        "warmup_source_job_ids": [str(row["id"]) for row in warmup_jobs],
        "inactive_source_job_ids": [str(row["id"]) for row in inactive_jobs],
    }


def snapshot_universe_config(reference_universe_run_id: UUID | str, snapshot_date: date) -> UniverseBuildConfig:
    """Clone the existing liquid-universe methodology using past data only."""
    ref = _reference_universe(reference_universe_run_id)
    selection = dict(ref.get("selection_config") or {})
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
    source_config = dict(ref.get("source_config") or {})
    if config.scope == "full_history":
        source_config["historical_source_readiness"] = json_safe(
            point_in_time_source_readiness(reference_universe_run_id, config.start_date, config.end_date)
        )
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM ra_point_in_time_universe_runs WHERE parent_job_id=%s", (job_id,))
            existing = cur.fetchone()
            if existing:
                run_id = existing["id"]
                cur.execute(
                    """
                    UPDATE ra_point_in_time_universe_runs SET status='running',latest_error=NULL,
                        requested_start=%s,requested_end=%s,methodology_version=%s,source_config=%s,completed_at=NULL,
                        started_at=COALESCE(started_at,now()) WHERE id=%s
                    """,
                    (config.start_date, config.end_date, PTI_UNIVERSE_VERSION, Jsonb(source_config), run_id),
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
                        Jsonb(source_config),
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
    readiness = point_in_time_source_readiness(reference_universe_run_id, config.start_date, config.end_date)
    if not readiness["ready"]:
        return []
    trading_dates = _spy_trading_dates(source, readiness["required_warmup_start"], config.end_date)
    research_dates = [d for d in trading_dates if config.start_date <= d <= config.end_date]

    eligible_dates: list[date] = []
    for candidate in research_dates:
        prior_count = sum(
            1
            for prior in trading_dates
            if candidate - timedelta(days=PTI_LOOKBACK_CALENDAR_DAYS) <= prior < candidate
        )
        if prior_count >= minimum_trading_days:
            eligible_dates.append(candidate)
    if not eligible_dates:
        return []

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
                    ON CONFLICT (point_in_time_universe_run_id,snapshot_date) DO UPDATE SET
                        effective_start=excluded.effective_start,
                        effective_end=excluded.effective_end,
                        lookback_start=excluded.lookback_start,
                        lookback_end=excluded.lookback_end
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
    """Create immutable-as-of monthly universe snapshots before feature work."""
    if config.scope == "full_history":
        readiness = point_in_time_source_readiness(reference_universe_run_id, config.start_date, config.end_date)
        if not readiness["ready"]:
            raise RuntimeError("Point-in-time historical source is not ready: " + " | ".join(readiness["blockers"]))
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
    if not snapshots:
        raise RuntimeError("No point-in-time universe snapshots were planned for the requested period.")
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
