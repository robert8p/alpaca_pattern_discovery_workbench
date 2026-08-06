from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb

from app.config import get_settings
from app.db import connection
from app.jobs import add_event, set_progress
from app.models import QualityScanConfig, timeframe_minutes
from app.sql_validation import validate_sql_bindings
from app.utils import json_safe, market_date_bounds


def _expected_bars(config: QualityScanConfig) -> int:
    base = timeframe_minutes(config.timeframe)
    session_minutes = {
        "regular": 390,
        "premarket": 330,
        "postmarket": 240,
        "overnight": 480,
        "all": 1440,
    }[config.session]
    return max(1, session_minutes // base)


def run_quality_scan(job_id: str, config: QualityScanConfig) -> dict[str, Any]:
    timeout = get_settings().database_statement_timeout_seconds
    start, end = config.start_date, config.end_date
    start_ts, end_ts = market_date_bounds(start, end)
    common = (config.timeframe, config.feed, config.adjustment, config.session, config.session, start_ts, end_ts)
    set_progress(job_id, "checking source tables", 0, 5)

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute(
                """
                SELECT to_regclass('public.rd_bars') AS bars,
                       to_regclass('public.rd_jobs') AS jobs,
                       to_regclass('public.rd_tasks') AS tasks,
                       to_regclass('public.rd_assets') AS assets
                """
            )
            tables = cur.fetchone()
            if not tables["bars"]:
                raise RuntimeError("rd_bars does not exist in this database. Point this app at the loader's Supabase database.")
        conn.rollback()

    set_progress(job_id, "inventory", 1, 5)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute(
                """
                SELECT session_label,count(*) AS bars,count(DISTINCT symbol) AS symbols,
                    min(bar_ts) AS first_bar,max(bar_ts) AS last_bar,
                    count(DISTINCT (bar_ts AT TIME ZONE 'America/New_York')::date) AS trading_dates
                FROM rd_bars
                WHERE timeframe=%s AND feed=%s AND adjustment=%s
                  AND (%s='all' OR session_label=%s)
                  AND bar_ts >= %s AND bar_ts < %s
                GROUP BY session_label ORDER BY session_label
                """,
                (config.timeframe, config.feed, config.adjustment, config.session, config.session, start, end),
            )
            inventory = cur.fetchall()
        conn.rollback()

    set_progress(job_id, "daily coverage", 2, 5)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute(
                """
                SELECT (bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date,
                    count(*) AS bars,count(DISTINCT symbol) AS symbols,
                    count(DISTINCT session_label) AS sessions
                FROM rd_bars
                WHERE timeframe=%s AND feed=%s AND adjustment=%s
                  AND (%s='all' OR session_label=%s)
                  AND bar_ts >= %s AND bar_ts < %s
                GROUP BY 1 ORDER BY 1
                """,
                common,
            )
            daily_coverage = cur.fetchall()
        conn.rollback()

    set_progress(job_id, "completeness", 3, 5)
    expected = _expected_bars(config)
    threshold = expected * config.complete_day_bar_tolerance_pct / 100.0
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute(
                """
                WITH per_day AS (
                    SELECT symbol,(bar_ts AT TIME ZONE 'America/New_York')::date AS trade_date,
                        count(*) AS bars
                    FROM rd_bars
                    WHERE timeframe=%s AND feed=%s AND adjustment=%s
                      AND (%s='all' OR session_label=%s)
                      AND bar_ts >= %s AND bar_ts < %s
                    GROUP BY symbol,2
                )
                SELECT CASE
                    WHEN bars >= %s THEN 'complete'
                    WHEN bars >= %s*0.75 THEN '75–95%%'
                    WHEN bars >= %s*0.25 THEN '25–75%%'
                    ELSE 'under 25%%' END AS coverage_band,
                    count(*) AS symbol_days
                FROM per_day GROUP BY 1 ORDER BY 1
                """,
                (*common, threshold, threshold, threshold),
            )
            completeness = cur.fetchall()
        conn.rollback()

    set_progress(job_id, "anomaly checks", 4, 5)
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(f"SET LOCAL statement_timeout = '{int(timeout)}s'")
            cur.execute(
                """
                SELECT
                    count(*) FILTER (WHERE open<=0 OR high<=0 OR low<=0 OR close<=0) AS nonpositive_prices,
                    count(*) FILTER (WHERE high<low OR high<greatest(open,close) OR low>least(open,close)) AS invalid_ohlc,
                    count(*) FILTER (WHERE volume<0 OR COALESCE(trade_count,0)<0) AS negative_activity,
                    count(*) FILTER (WHERE volume=0) AS zero_volume_bars,
                    count(*) AS checked_bars
                FROM rd_bars
                WHERE timeframe=%s AND feed=%s AND adjustment=%s
                  AND (%s='all' OR session_label=%s)
                  AND bar_ts >= %s AND bar_ts < %s
                """,
                common,
            )
            anomalies = dict(cur.fetchone())
            cur.execute(
                """
                WITH days AS (
                    SELECT symbol,count(DISTINCT (bar_ts AT TIME ZONE 'America/New_York')::date) AS n
                    FROM rd_bars
                    WHERE timeframe=%s AND feed=%s AND adjustment=%s
                      AND (%s='all' OR session_label=%s)
                      AND bar_ts >= %s AND bar_ts < %s
                    GROUP BY symbol
                )
                SELECT count(*) FILTER (WHERE n<%s) AS symbols_below_minimum_days FROM days
                """,
                (*common, config.minimum_symbol_days),
            )
            anomalies.update(cur.fetchone())
            ingestion = {}
            if tables["jobs"] and tables["tasks"]:
                cur.execute(
                    """
                    SELECT count(*) FILTER (WHERE status='completed') AS completed_jobs,
                        count(*) FILTER (WHERE status='failed') AS failed_jobs,
                        count(*) FILTER (WHERE status IN ('queued','planning','running','pause_requested')) AS active_jobs
                    FROM rd_jobs
                    """
                )
                ingestion = dict(cur.fetchone())
        conn.rollback()

    summary = {
        "source": config.model_dump(mode="json"),
        "expected_bars_per_symbol_day": expected,
        "complete_day_threshold": threshold,
        "bars": sum(int(row["bars"]) for row in inventory),
        "symbols": max((int(row["symbols"]) for row in inventory), default=0),
        "trading_dates": len(daily_coverage),
        "ingestion": ingestion,
    }
    payload = {
        "summary": summary,
        "session_inventory": inventory,
        "daily_coverage": daily_coverage,
        "completeness_bands": completeness,
        "anomalies": anomalies,
    }
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO ra_quality_reports(job_id,name,source_config,summary,session_inventory,daily_coverage,completeness_bands,anomalies)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(job_id) DO UPDATE SET name=excluded.name,source_config=excluded.source_config,
                    summary=excluded.summary,session_inventory=excluded.session_inventory,
                    daily_coverage=excluded.daily_coverage,completeness_bands=excluded.completeness_bands,
                    anomalies=excluded.anomalies,created_at=now()
                RETURNING id
                """,
                (
                    job_id, config.name, Jsonb(config.model_dump(mode="json")), Jsonb(json_safe(summary)),
                    Jsonb(json_safe(inventory)), Jsonb(json_safe(daily_coverage)),
                    Jsonb(json_safe(completeness)), Jsonb(json_safe(anomalies)),
                ),
            )
            report_id = cur.fetchone()["id"]
        conn.commit()
    add_event(job_id, "quality_report_created", "Data-quality report created.", details={"report_id": report_id})
    set_progress(job_id, "complete", 5, 5)
    return {"quality_report_id": report_id, **json_safe(summary)}
